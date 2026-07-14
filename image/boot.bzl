"""Boot filesystem layers assembled from standalone boot artifacts."""

load("//engine:rules.bzl", "EngineInfo", "chroot_run")
load(":layer.bzl", "ImageInfo")

def _boot_layer_impl(ctx: AnalysisContext) -> list[Provider]:
    parent = ctx.attrs.parent[ImageInfo]
    delta = ctx.actions.declare_output("delta", dir = True)
    work = ctx.actions.declare_output("overlay.work", dir = True)
    cmd = cmd_args(
        chroot_run(engine = parent.engine[EngineInfo], exe = ctx.attrs._driver),
        "--out",
        delta.as_output(),
        "--work",
        work.as_output(),
    )
    if ctx.attrs.bootloader == "systemd-boot":
        cmd.add("--systemd-boot")
    for lower in parent.layers:
        cmd.add("--lower", lower)
    for destination, source in ctx.attrs.files.items():
        cmd.add("--file", source, destination)
    ctx.actions.run(cmd, category = "boot_layer")

    return [
        DefaultInfo(default_output = delta),
        ImageInfo(
            engine = parent.engine,
            layers = parent.layers + [delta],
            tmpfiles = parent.tmpfiles,
        ),
    ]

boot_layer = rule(
    impl = _boot_layer_impl,
    attrs = {
        "parent": attrs.dep(providers = [ImageInfo], doc = "the logical image to add boot artifacts to"),
        "files": attrs.dict(
            key = attrs.string(),
            value = attrs.source(),
            default = {},
            doc = "absolute image destination to boot artifact mapping",
        ),
        "bootloader": attrs.option(
            attrs.enum(["systemd-boot"]),
            default = None,
            doc = "bootloader to install into the image",
        ),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image:boot"),
    },
)
