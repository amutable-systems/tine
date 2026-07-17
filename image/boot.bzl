"""Boot artifacts and operation groups."""

load("//image_format:archive.bzl", "CpioArchiveInfo")
load("//image_format:disk.bzl", "RootHashInfo")
load(":actions.bzl", "terminal_image_command")
load(
    ":layer.bzl",
    "ImageInfo",
    "LayerOperation",  # @unused Used as a type.
    "mkdir",
    "remove",
    "run",
)

def install_systemd_boot() -> list[LayerOperation]:
    """Return operations that install systemd-boot into the image ESP staging paths."""
    return [
        mkdir("/efi"),
        run(
            [
                "bootctl",
                "install",
                "--root=/buildroot",
                "--install-source=image",
                "--all-architectures",
                "--no-variables",
            ],
            chroot = False,
            env = {
                "SYSTEMD_ESP_PATH": "/efi",
                "SYSTEMD_XBOOTLDR_PATH": "/boot",
            },
        ),
        remove("/efi/loader/random-seed"),
    ]

# buildifier: disable=name-conventions  (record type, conventionally UpperCamelCase)
UkiProfile = record(
    id = str,
    title = str,
    cmdline = list[str],
)

# buildifier: disable=function-docstring-args
# buildifier: disable=function-docstring-return
def uki_profile(id: str, title: str, cmdline: list[str]) -> UkiProfile:
    """Describe one alternative boot profile embedded in a UKI."""

    # sd-boot derives entry identifiers from the id, so keep it filename- and env-file-safe.
    if not regex_match("^[a-z0-9._-]+$", id):
        fail("uki_profile: invalid id {!r}".format(id))
    if not title or "\n" in title:
        fail("uki_profile: title must be a single non-empty line")
    for argument in cmdline:
        if not argument:
            fail("uki_profile: cmdline arguments cannot be empty")
    return UkiProfile(id = id, title = title, cmdline = cmdline)

def _uki_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    out = ctx.actions.declare_output("ukis", dir = True)
    cmd = terminal_image_command(image, ctx.attrs._driver)
    cmd.add(
        "--out",
        out.as_output(),
        "--arch",
        ctx.attrs.arch,
    )
    image_id = ctx.attrs.image_id if ctx.attrs.image_id != None else ctx.label.name
    version = ctx.attrs.version if ctx.attrs.version != None else "0"
    for value in (image_id, version):
        if not regex_match("^[a-zA-Z0-9._~-]+$", value):
            fail("uki: invalid filename component {!r}".format(value))
    cmd.add("--image-id", image_id, "--version", version)
    for argument in ctx.attrs.cmdline:
        cmd.add("--cmdline", argument)
    for profile in ctx.attrs.profiles:
        cmd.add("--profile", profile)
    if ctx.attrs.root_hash != None:
        root_hash = ctx.attrs.root_hash[RootHashInfo]
        cmd.add("--root-hash", root_hash.hash, "--root-hash-kind", root_hash.kind)
    for initrd in ctx.attrs.initrds:
        cmd.add("--initrd", initrd[CpioArchiveInfo].archive)
    ctx.actions.run(cmd, category = "uki")
    return [DefaultInfo(default_output = out)]

_uki = rule(
    impl = _uki_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the image supplying the kernel, modules, stub, and os-release"),
        "initrds": attrs.list(
            attrs.dep(providers = [CpioArchiveInfo]),
            default = [],
            doc = "cpio archives to prepend to the per-kernel modules archive",
        ),
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
        "arch": attrs.enum(["x86_64"], default = "x86_64"),
        "image_id": attrs.option(
            attrs.string(),
            default = None,
            doc = "first component of the UKI name <image_id>_<version>_<arch>.efi; defaults to the target name",
        ),
        "version": attrs.option(attrs.string(), default = None, doc = "image version in the UKI name, default 0"),
        "root_hash": attrs.option(
            attrs.dep(providers = [RootHashInfo]),
            default = None,
            doc = "verity hash to add to the embedded kernel command line",
        ),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image:uki"),
    },
)

def uki(name: str, profiles: list[UkiProfile] = [], **kwargs) -> None:
    """Build UKIs, with each profile added as an alternative sd-boot menu entry.

    A profile's cmdline is appended to the base cmdline (kernel arguments are last-wins, so
    profiles can also override it).
    """
    ids = {}
    for profile in profiles:
        if profile.id in ids:
            fail("uki: duplicate profile id {!r}".format(profile.id))
        ids[profile.id] = True
    _uki(
        name = name,
        profiles = [
            json.encode({"id": profile.id, "title": profile.title, "cmdline": profile.cmdline})
            for profile in profiles
        ],
        **kwargs
    )

def _bootable_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    selection = ctx.actions.declare_output("boot-artifacts.json")
    select = terminal_image_command(image, ctx.attrs._selector)
    select.add("--out", selection.as_output())
    ctx.actions.run(select, category = "boot_artifact_select")

    artifacts = {}
    sub_targets = {}
    for kind, filename in {
        "uki": "image.efi",
        "kernel": "vmlinuz",
        "initrd": "initrd",
    }.items():
        out = ctx.actions.declare_output(filename)
        extract = terminal_image_command(image, ctx.attrs._extractor)
        extract.add(
            "--manifest",
            selection,
            "--artifact",
            kind,
            "--out",
            out.as_output(),
        )
        ctx.actions.run(extract, category = "boot_artifact_" + kind)
        artifacts[kind] = out
        sub_targets[kind] = [DefaultInfo(default_output = out)]

    # A UKI is not guaranteed to exist (kernels can ship as plain files), so the default
    # outputs stay limited to the artifacts selection always yields; [uki] extracts on demand.
    return [
        DefaultInfo(
            default_outputs = [artifacts["kernel"], artifacts["initrd"]],
            sub_targets = sub_targets,
        ),
    ]

bootable = rule(
    impl = _bootable_impl,
    attrs = {
        "image": attrs.dep(
            providers = [ImageInfo],
            doc = "the completed logical image from which to select boot artifacts",
        ),
        "_extractor": attrs.dep(providers = [RunInfo], default = "tine//image:artifacts"),
        "_selector": attrs.dep(providers = [RunInfo], default = "tine//image:boot"),
    },
)
