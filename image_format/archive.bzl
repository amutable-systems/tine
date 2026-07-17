"""Archive and materialized-directory image outputs."""

load("//image:actions.bzl", "terminal_image_command")
load("//image:layer.bzl", "ImageInfo")

_EXT = {"tar": "tar", "cpio": "cpio"}
COMPRESSIONS = ["none", "zstd"]
_COMPRESSION_EXT = {"none": "", "zstd": ".zst"}

CpioArchiveInfo = provider(
    doc = "A newc CPIO archive, compressed as declared.",
    fields = {
        "archive": provider_field(Artifact),
    },
)

def _image_archive_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    out = ctx.actions.declare_output(
        "image." + _EXT[ctx.attrs.format] + _COMPRESSION_EXT[ctx.attrs.compression],
    )
    cmd = terminal_image_command(image, ctx.attrs._driver)
    cmd.add(
        "--out",
        out.as_output(),
        "--format",
        ctx.attrs.format,
    )
    if ctx.attrs.compression != "none":
        cmd.add("--compression", ctx.attrs.compression)
    ctx.actions.run(cmd, category = "image_" + ctx.attrs.format)

    providers = [DefaultInfo(default_output = out)]
    if ctx.attrs.format == "cpio":
        providers.append(CpioArchiveInfo(archive = out))
    return providers

image_archive = rule(
    impl = _image_archive_impl,
    attrs = {
        "compression": attrs.enum(COMPRESSIONS, default = "none"),
        "format": attrs.enum(["tar", "cpio"], default = "tar"),
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to archive"),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:archive"),
    },
)

def _image_directory_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    out = ctx.actions.declare_output("image.rootfs", dir = True)
    cmd = terminal_image_command(image, ctx.attrs._driver)
    cmd.add(
        "--out",
        out.as_output(),
        "--format",
        "directory",
    )
    ctx.actions.run(cmd, category = "image_directory")
    return [DefaultInfo(default_output = out)]

image_directory = rule(
    impl = _image_directory_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to materialize"),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:archive"),
    },
)
