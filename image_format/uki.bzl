# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Unified kernel images built from a logical image's kernel, modules, and stub."""

load(
    "//image:image.bzl",
    "ARCHES",
    "FILENAME_PATTERN",
    "IMAGE_TOOLS_ATTR",
    "ImageInfo",
    "ImageToolsInfo",
    "check_name",
    "check_version",
    "declare_out",
    "terminal_image_command",
)
load("//image:initrd_modules.toml", _INITRD_MODULES = "value")
load(
    "//image:sign.bzl",
    "SigningKeyInfo",
    "external_signing_execution",
    "merge_signing_access",
    "resolve_signing_key",
    "signing_key_spec",
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
        "modules": provider_field(Artifact),
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
    tools: ImageToolsInfo,
    image: ImageInfo,
    initrds: list[ImageArchiveInfo],
    cmdline: list[str],
    profiles: list[str],
    arch: str,
    image_id: str,
    version: str,
    initrd_modules: list[str],
    splash: Artifact | None = None,
    root_hash: RootHashInfo | None = None,
    secure_boot_key: SigningKeyInfo | None = None,
    sign_expected_pcr_key: SigningKeyInfo | None = None,
    identifier: str | None = None,
) -> UkiInfo:
    """Declare UKI generation from resolved image providers."""
    if sign_expected_pcr_key != None and secure_boot_key == None:
        fail("uki: sign_expected_pcr_key needs Secure Boot signing, which signs the UKI it seals")

    # ukify has one pair of provider options for both keys, so it cannot load one from a provider and
    # read the other from a file. Private keys and certificates are compared apart, because that is
    # how ukify names them: one key in a token and one certificate in a file is the same arrangement
    # for both roles or for neither.
    if sign_expected_pcr_key != None:
        for what, secure_boot_source, pcr_source in (
            ("private keys", secure_boot_key.private_key_source, sign_expected_pcr_key.private_key_source),
            ("certificates", secure_boot_key.certificate_source, sign_expected_pcr_key.certificate_source),
        ):
            if secure_boot_source != pcr_source:
                fail(
                    "uki: the Secure Boot and expected-PCR {} must come from the same source, got {!r} and {!r}".format(
                        what,
                        secure_boot_source,
                        pcr_source,
                    ),
                )
    out = declare_out(ctx, identifier, "ukis", dir = True)
    # Declared next to the UKIs, not inside them: that directory is copied onto the ESP whole.
    modules = declare_out(ctx, identifier, "modules.json")
    check_name("uki image_id", image_id, FILENAME_PATTERN)
    check_version("uki version", version)
    for initrd in initrds:
        if initrd.format != "cpio":
            fail("uki: initrd must be a cpio archive, got {!r}".format(initrd.format))

    signing_access = merge_signing_access([secure_boot_key, sign_expected_pcr_key])
    cmd = terminal_image_command(
        ctx,
        signing_access = signing_access,
        driver = "uki",
        exe = tools.uki,
        identifier = identifier,
        image = image,
        spec = {
            "cmdline": cmdline,
            "efi_arch": ARCHES[arch].efi,
            "image_id": image_id,
            "initrd_modules": initrd_modules,
            "initrds": [initrd.archive for initrd in initrds],
            "modules_manifest": modules.as_output(),
            "out": out.as_output(),
            "profiles": [json.decode(profile) for profile in profiles],
            "root_hash": {
                "kind": root_hash.kind,
                "path": root_hash.hash,
            }
            if root_hash != None
            else None,
            "secure_boot": signing_key_spec(secure_boot_key),
            "sign_expected_pcr": signing_key_spec(sign_expected_pcr_key),
            "splash": splash,
            "systemd_arch": ARCHES[arch].systemd,
            "version": version,
        },
    )
    ctx.actions.run(cmd, category = "uki", identifier = identifier or "uki", **external_signing_execution(signing_access))
    return UkiInfo(modules = modules, ukis = out)

def uki_subtargets(info: UkiInfo) -> dict[str, list[Provider]]:
    """The selection manifest, exposed wherever the UKIs themselves are."""
    return {"modules": [DefaultInfo(default_output = info.modules)]}

def _uki_impl(ctx: AnalysisContext) -> list[Provider]:
    root_hash = None
    if ctx.attrs.root_hash != None:
        root_hash = ctx.attrs.root_hash[RepartInfo].root_hash
        if root_hash == None:
            fail("uki: root_hash RepartInfo does not contain a verity root hash")
    info = declare_uki(
        ctx,
        tools = ctx.attrs._tools[ImageToolsInfo],
        arch = ctx.attrs.arch,
        cmdline = ctx.attrs.cmdline,
        image = ctx.attrs.image[ImageInfo],
        image_id = ctx.attrs.image_id if ctx.attrs.image_id != None else ctx.label.name,
        initrd_modules = ctx.attrs.initrd_modules,
        initrds = [initrd[ImageArchiveInfo] for initrd in ctx.attrs.initrds],
        profiles = ctx.attrs.profiles,
        root_hash = root_hash,
        secure_boot_key = resolve_signing_key(ctx.attrs.secure_boot_key),
        sign_expected_pcr_key = resolve_signing_key(ctx.attrs.sign_expected_pcr_key),
        splash = ctx.attrs.splash,
        version = ctx.attrs.version,
    )
    return [DefaultInfo(default_output = info.ukis, sub_targets = uki_subtargets(info)), info]

UKI_ATTRS = {
    "arch": attrs.enum(ARCHES.keys(), default = "x86_64"),
    "cmdline": attrs.list(
        attrs.string(),
        default = [],
        doc = "kernel command-line arguments embedded in the UKI",
    ),
    "initrd_modules": attrs.list(
        attrs.string(),
        default = _INITRD_MODULES["patterns"],
        doc = "glob patterns selecting the kernel modules the UKI's per-kernel initrd carries",
    ),
    "profiles": attrs.list(
        attrs.string(),
        default = [],
        doc = "serialized alternative boot profiles",
    ),
    "secure_boot_key": attrs.option(
        attrs.dep(providers = [SigningKeyInfo]),
        default = None,
        doc = "key signing the UKI and its kernel",
    ),
    "sign_expected_pcr_key": attrs.option(
        attrs.dep(providers = [SigningKeyInfo]),
        default = None,
        doc = "key sealing the expected-PCR policy; without one the policy is not sealed",
    ),
    "splash": attrs.option(
        attrs.source(),
        default = None,
        doc = "BMP image embedded as the UKI splash screen",
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
    profiles can also override it). With secure_boot_key, the UKI and its embedded kernel are signed
    for Secure Boot. With sign_expected_pcr_key, a signed expected-PCR policy covers every profile
    that does not opt out.
    """
    _uki(name = name, profiles = encode_profiles(profiles), **kwargs)
