"""Archive and materialized-directory image outputs."""

load("//engine:rules.bzl", "EngineInfo", "chroot_run")
load("//image:layer.bzl", "ImageInfo")

_EXT = {"tar": "tar", "cpio": "cpio"}

def _archive_action(ctx: AnalysisContext, format: str, out: OutputArtifact) -> None:
    image = ctx.attrs.image[ImageInfo]
    cmd = cmd_args(
        chroot_run(engine = image.engine[EngineInfo], exe = ctx.attrs._driver),
        "--out",
        out,
        "--format",
        format,
    )
    for lower in image.layers:
        cmd.add("--lower", lower)
    for snippet in image.tmpfiles:
        cmd.add("--tmpfiles", snippet)
    ctx.actions.run(cmd, category = "image_" + format)

def _image_archive_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output("image." + _EXT[ctx.attrs.format])
    _archive_action(ctx, ctx.attrs.format, out.as_output())
    return [DefaultInfo(default_output = out)]

image_archive = rule(
    impl = _image_archive_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to archive"),
        "format": attrs.enum(["tar", "cpio"], default = "tar"),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:archive"),
    },
)

def _image_directory_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output("image.rootfs", dir = True)
    _archive_action(ctx, "directory", out.as_output())
    return [DefaultInfo(default_output = out)]

image_directory = rule(
    impl = _image_directory_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to materialize"),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:archive"),
    },
)
