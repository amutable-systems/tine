"""Unified kernel images built from a logical image's kernel, modules, and stub."""

load(
    "//image:image.bzl",
    "ARCHES",
    "FILENAME_PATTERN",
    "IMAGE_TOOLS_ATTR",
    "ImageInfo",
    "ImageToolsInfo",
    "VERSION_PATTERN",
    "check_name",
    "declare_out",
    "terminal_image_command",
)
load(":archive.bzl", "ImageArchiveInfo")
load(
    ":disk.bzl",
    "RepartInfo",
    "RootHashInfo",  # @unused Used as a function argument type.
)

UkiInfo = provider(
    doc = "Unified kernel images built for one logical image.",
    fields = {
        "ukis": provider_field(Artifact),
    },
)

UkiProfile = dict

def uki_profile(id: str, title: str, cmdline: list[str], sign_expected_pcr: bool = True) -> UkiProfile:
    """Describe one alternative boot profile embedded in a UKI.

    Profiles whose boot state is not sealed against (e.g. installers or factory reset) can opt
    out of the expected-PCR policy with sign_expected_pcr = False.
    """

    # sd-boot derives entry identifiers from the id, so keep it filename- and env-file-safe.
    check_name("uki_profile id", id, "^[a-z0-9._-]+$")
    if not title or "\n" in title:
        fail("uki_profile: title must be a single non-empty line")
    for argument in cmdline:
        if not argument:
            fail("uki_profile: cmdline arguments cannot be empty")
    return {
        "cmdline": cmdline,
        "id": id,
        "sign_expected_pcr": sign_expected_pcr,
        "title": title,
    }

def encode_profiles(profiles: list[UkiProfile]) -> list[str]:
    """Reject duplicate profile ids and serialize the profiles for an attribute."""
    ids = {}
    for profile in profiles:
        if profile["id"] in ids:
            fail("uki: duplicate profile id {!r}".format(profile["id"]))
        ids[profile["id"]] = True
    return [json.encode(profile) for profile in profiles]

def declare_uki(
    ctx: AnalysisContext,
    *,
    image: ImageInfo,
    initrds: list[ImageArchiveInfo],
    cmdline: list[str],
    profiles: list[str],
    arch: str,
    image_id: str,
    version: str,
    root_hash: RootHashInfo | None = None,
    secure_boot_private_key: Artifact | None = None,
    secure_boot_certificate: Artifact | None = None,
    identifier: str | None = None,
) -> UkiInfo:
    """Declare UKI generation from resolved image providers."""
    if (secure_boot_private_key == None) != (secure_boot_certificate == None):
        fail("uki: secure_boot_private_key and secure_boot_certificate must be specified together")
    out = declare_out(ctx, identifier, "ukis", dir = True)
    check_name("uki image_id", image_id, FILENAME_PATTERN)
    check_name("uki version", version, VERSION_PATTERN)
    for initrd in initrds:
        if initrd.format != "cpio":
            fail("uki: initrd must be a cpio archive, got {!r}".format(initrd.format))

    secure_boot = None
    if secure_boot_private_key != None:
        secure_boot = {
            "certificate": secure_boot_certificate,
            "private_key": secure_boot_private_key,
        }
    cmd = terminal_image_command(
        ctx,
        driver = "uki",
        exe = ctx.attrs._tools[ImageToolsInfo].uki,
        identifier = identifier,
        image = image,
        spec = {
            "cmdline": cmdline,
            "efi_arch": ARCHES[arch].efi,
            "image_id": image_id,
            "initrds": [initrd.archive for initrd in initrds],
            "out": out.as_output(),
            "profiles": [json.decode(profile) for profile in profiles],
            "root_hash": {
                "kind": root_hash.kind,
                "path": root_hash.hash,
            }
            if root_hash != None
            else None,
            "secure_boot": secure_boot,
            "systemd_arch": ARCHES[arch].systemd,
            "version": version,
        },
    )
    ctx.actions.run(cmd, category = "uki", identifier = identifier or "uki")
    return UkiInfo(ukis = out)

def _uki_impl(ctx: AnalysisContext) -> list[Provider]:
    root_hash = None
    if ctx.attrs.root_hash != None:
        root_hash = ctx.attrs.root_hash[RepartInfo].root_hash
        if root_hash == None:
            fail("uki: root_hash RepartInfo does not contain a verity root hash")
    info = declare_uki(
        ctx,
        arch = ctx.attrs.arch,
        cmdline = ctx.attrs.cmdline,
        image = ctx.attrs.image[ImageInfo],
        image_id = ctx.attrs.image_id if ctx.attrs.image_id != None else ctx.label.name,
        initrds = [initrd[ImageArchiveInfo] for initrd in ctx.attrs.initrds],
        profiles = ctx.attrs.profiles,
        root_hash = root_hash,
        secure_boot_certificate = ctx.attrs.secure_boot_certificate,
        secure_boot_private_key = ctx.attrs.secure_boot_private_key,
        version = ctx.attrs.version,
    )
    return [DefaultInfo(default_output = info.ukis), info]

UKI_ATTRS = {
    "arch": attrs.enum(ARCHES.keys(), default = "x86_64"),
    "cmdline": attrs.list(
        attrs.string(),
        default = [],
        doc = "kernel command-line arguments embedded in the UKI",
    ),
    "profiles": attrs.list(
        attrs.string(),
        default = [],
        doc = "serialized alternative boot profiles",
    ),
    "secure_boot_certificate": attrs.option(
        attrs.source(),
        default = None,
        doc = "PEM certificate for Secure Boot signing",
    ),
    "secure_boot_private_key": attrs.option(
        attrs.source(),
        default = None,
        doc = "PEM key for Secure Boot and expected-PCR signing",
    ),
}

_uki = rule(
    impl = _uki_impl,
    attrs = UKI_ATTRS
    | {
        "image": attrs.dep(
            providers = [ImageInfo],
            doc = "the image supplying the kernel, modules, stub, and os-release",
        ),
        "image_id": attrs.option(
            attrs.string(),
            default = None,
            doc = "first component of the UKI name <image_id>_<version>_<arch>.efi; defaults to the target name",
        ),
        "initrds": attrs.list(
            attrs.dep(providers = [ImageArchiveInfo]),
            default = [],
            doc = "cpio archives to prepend to the per-kernel modules archive",
        ),
        "root_hash": attrs.option(
            attrs.dep(providers = [RepartInfo]),
            default = None,
            doc = "repart result whose verity hash is added to the embedded kernel command line",
        ),
        "version": attrs.string(default = "0", doc = "image version in the UKI name"),
    }
    | IMAGE_TOOLS_ATTR,
)

def uki(name: str, profiles: list[UkiProfile] = [], **kwargs) -> None:
    """Build UKIs, with each profile added as an alternative sd-boot menu entry.

    A profile's cmdline is appended to the base cmdline (kernel arguments are last-wins, so
    profiles can also override it). With secure_boot_private_key/_certificate, the UKI and its
    embedded kernel are signed for Secure Boot and a signed expected-PCR policy covers every
    profile that does not opt out.
    """
    if (kwargs.get("secure_boot_private_key") == None) != (kwargs.get("secure_boot_certificate") == None):
        fail("uki: secure_boot_private_key and secure_boot_certificate must be specified together")
    _uki(name = name, profiles = encode_profiles(profiles), **kwargs)
