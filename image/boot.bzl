"""Boot artifacts and operation groups."""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load("//image_format:archive.bzl", "CpioArchiveInfo")
load("//image_format:disk.bzl", "RootHashInfo")
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

BootableImageInfo = provider(
    doc = "A logical image with a selected kernel and matching initrd.",
    fields = {
        "engine": provider_field(Dependency),
        "initrd": provider_field(Artifact),
        "kernel": provider_field(Artifact),
        "source": provider_field(Dependency),
        "uki": provider_field(Artifact),
    },
)

def _uki_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    out = ctx.actions.declare_output("ukis", dir = True)
    cmd = cmd_args(
        chroot_run(engine = image.engine[EngineInfo], exe = ctx.attrs._driver),
        "--out",
        out.as_output(),
        "--arch",
        ctx.attrs.arch,
        "--entry",
        ctx.attrs.entry,
    )
    for argument in ctx.attrs.cmdline:
        cmd.add("--cmdline", argument)
    if ctx.attrs.root_hash != None:
        root_hash = ctx.attrs.root_hash[RootHashInfo]
        cmd.add("--root-hash", root_hash.hash, "--root-hash-kind", root_hash.kind)
    for lower in image.layers:
        cmd.add("--lower", lower)
    for initrd in ctx.attrs.initrds:
        cmd.add("--initrd", initrd[CpioArchiveInfo].archive)
    ctx.actions.run(cmd, category = "uki")
    return [DefaultInfo(default_output = out)]

uki = rule(
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
        "arch": attrs.enum(["x86_64"], default = "x86_64"),
        "entry": attrs.string(default = "linux", doc = "filename prefix for the generated UKIs"),
        "root_hash": attrs.option(
            attrs.dep(providers = [RootHashInfo]),
            default = None,
            doc = "verity hash to add to the embedded kernel command line",
        ),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image:uki"),
    },
)

def _bootable_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    selection = ctx.actions.declare_output("boot-artifacts.json")
    select = cmd_args(
        chroot_run(engine = image.engine[EngineInfo]),
        ctx.attrs._artifacts_driver[RunInfo],
        "select",
        "--out",
        selection.as_output(),
    )
    for lower in image.layers:
        select.add("--lower", lower)
    ctx.actions.run(select, category = "boot_artifact_select")

    artifacts = {}
    sub_targets = {}
    for kind, filename in {
        "uki": "image.efi",
        "kernel": "vmlinuz",
        "initrd": "initrd",
    }.items():
        out = ctx.actions.declare_output(filename)
        extract = cmd_args(
            chroot_run(engine = image.engine[EngineInfo]),
            ctx.attrs._artifacts_driver[RunInfo],
            "extract",
            "--manifest",
            selection,
            "--kind",
            kind,
            "--out",
            out.as_output(),
        )
        for lower in image.layers:
            extract.add("--lower", lower)
        ctx.actions.run(extract, category = "boot_artifact_" + kind)
        artifacts[kind] = out
        sub_targets[kind] = [DefaultInfo(default_output = out)]

    return [
        DefaultInfo(
            default_outputs = [artifacts["kernel"], artifacts["initrd"]],
            sub_targets = sub_targets,
        ),
        BootableImageInfo(
            engine = image.engine,
            initrd = artifacts["initrd"],
            kernel = artifacts["kernel"],
            source = ctx.attrs.image,
            uki = artifacts["uki"],
        ),
    ]

bootable = rule(
    impl = _bootable_impl,
    attrs = {
        "image": attrs.dep(
            providers = [ImageInfo],
            doc = "the completed logical image from which to select boot artifacts",
        ),
        "_artifacts_driver": attrs.dep(providers = [RunInfo], default = "tine//image:boot-artifacts"),
    },
)
