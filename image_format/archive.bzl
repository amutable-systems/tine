"""Archive and materialized-directory image outputs."""

load(
    "//image:image.bzl",
    "IMAGE_TOOLS_ATTR",
    "ImageInfo",
    "ImageToolsInfo",
    "declare_out",
    "terminal_image_command",
)

_EXT = {"cpio": "cpio", "tar": "tar"}
COMPRESSIONS = ["none", "zstd"]
_COMPRESSION_EXT = {"none": "", "zstd": ".zst"}

ImageArchiveInfo = provider(
    doc = "A deterministic tar or newc cpio archive of a logical image, compressed as declared.",
    fields = {
        "archive": provider_field(Artifact),
        "format": provider_field(str),
    },
)

ImageDirectoryInfo = provider(
    doc = "A materialized logical image directory.",
    fields = {
        "directory": provider_field(Artifact),
    },
)

# buildifier: disable=function-docstring-args
# buildifier: disable=function-docstring-return
def declare_image_archive(
        ctx: AnalysisContext,
        *,
        image: ImageInfo,
        format: str,
        compression: str,
        identifier: str | None = None) -> ImageArchiveInfo:
    """Declare an archive action from a resolved logical image."""
    out = declare_out(
        ctx,
        identifier,
        "image." + _EXT[format] + _COMPRESSION_EXT[compression],
    )
    cmd = terminal_image_command(image, ctx.attrs._tools[ImageToolsInfo].archive)
    cmd.add("--out", out.as_output(), "--format", format)
    if compression != "none":
        cmd.add("--compression", compression)
    ctx.actions.run(cmd, category = "image_" + format, identifier = identifier or format)
    return ImageArchiveInfo(archive = out, format = format)

def _image_archive_impl(ctx: AnalysisContext) -> list[Provider]:
    info = declare_image_archive(
        ctx,
        compression = ctx.attrs.compression,
        format = ctx.attrs.format,
        image = ctx.attrs.image[ImageInfo],
    )
    return [DefaultInfo(default_output = info.archive), info]

ARCHIVE_ATTRS = {
    "compression": attrs.enum(COMPRESSIONS, default = "none"),
    "format": attrs.enum(["tar", "cpio"], default = "tar"),
}

image_archive = rule(
    impl = _image_archive_impl,
    attrs = ARCHIVE_ATTRS | {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to archive"),
    } | IMAGE_TOOLS_ATTR,
)

# buildifier: disable=function-docstring-args
# buildifier: disable=function-docstring-return
def declare_image_directory(
        ctx: AnalysisContext,
        *,
        image: ImageInfo,
        identifier: str | None = None) -> ImageDirectoryInfo:
    """Declare a directory materialization from a resolved logical image."""
    out = declare_out(ctx, identifier, "image.rootfs", dir = True)
    cmd = terminal_image_command(image, ctx.attrs._tools[ImageToolsInfo].archive)
    cmd.add("--out", out.as_output(), "--format", "directory")
    ctx.actions.run(cmd, category = "image_directory", identifier = identifier or "directory")
    return ImageDirectoryInfo(directory = out)

def _image_directory_impl(ctx: AnalysisContext) -> list[Provider]:
    info = declare_image_directory(ctx, image = ctx.attrs.image[ImageInfo])
    return [DefaultInfo(default_output = info.directory), info]

image_directory = rule(
    impl = _image_directory_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to materialize"),
    } | IMAGE_TOOLS_ATTR,
)
