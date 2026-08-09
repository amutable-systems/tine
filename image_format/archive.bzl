"""Archive and materialized-directory image outputs."""

load("//distribution:defs.bzl", "distributed")
load(
    "//image:image.bzl",
    "IMAGE_TOOLS_ATTR",
    "ImageInfo",
    "ImageToolsInfo",
    "declare_out",
    "pkgdb_paths",
    "terminal_image_command",
)

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

def declare_image_archive(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    image: ImageInfo,
    format: str,
    compression: str,
    strip_pkgdb: bool = False,
    identifier: str | None = None,
) -> ImageArchiveInfo:
    """Declare an archive action from a resolved logical image.

    With strip_pkgdb, the archive omits the package database wherever the image's package system
    keeps it, for a root nothing ever resolves packages in. The image's `[pkgdb]` subtarget still
    captures the database from the tree itself.
    """
    out = declare_out(ctx, identifier, "image." + format + _COMPRESSION_EXT[compression])
    cmd = terminal_image_command(
        ctx,
        # One image can be archived in several formats, so the spec is named like the output.
        driver = "archive-" + format,
        exe = tools.archive,
        identifier = identifier,
        image = image,
        spec = {
            "compression": compression,
            "format": format,
            "out": out.as_output(),
            "pkgdb_paths": pkgdb_paths(image) if strip_pkgdb else [],
        },
    )
    ctx.actions.run(cmd, category = "image_" + format, identifier = identifier or format)
    return ImageArchiveInfo(archive = out, format = format)

def _image_archive_impl(ctx: AnalysisContext) -> list[Provider]:
    info = declare_image_archive(
        ctx,
        tools = ctx.attrs._tools[ImageToolsInfo],
        compression = ctx.attrs.compression,
        format = ctx.attrs.format,
        image = ctx.attrs.image[ImageInfo],
    )
    return [DefaultInfo(default_output = info.archive), info]

ARCHIVE_ATTRS = {
    "compression": attrs.enum(COMPRESSIONS, default = "none"),
    "format": attrs.enum(["tar", "cpio"], default = "tar"),
}

_image_archive = rule(
    impl = _image_archive_impl,
    attrs = ARCHIVE_ATTRS
    | {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to archive"),
    }
    | IMAGE_TOOLS_ATTR,
)

def declare_image_directory(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    image: ImageInfo,
    identifier: str | None = None,
) -> ImageDirectoryInfo:
    """Declare a directory materialization from a resolved logical image."""
    out = declare_out(ctx, identifier, "image.rootfs", dir = True)
    cmd = terminal_image_command(
        ctx,
        driver = "directory",
        exe = tools.archive,
        identifier = identifier,
        image = image,
        spec = {
            "compression": "none",
            "format": "directory",
            "out": out.as_output(),
            "pkgdb_paths": [],
        },
    )
    ctx.actions.run(cmd, category = "image_directory", identifier = identifier or "directory")
    return ImageDirectoryInfo(directory = out)

def _image_directory_impl(ctx: AnalysisContext) -> list[Provider]:
    info = declare_image_directory(ctx, tools = ctx.attrs._tools[ImageToolsInfo], image = ctx.attrs.image[ImageInfo])
    return [DefaultInfo(default_output = info.directory), info]

_image_directory = rule(
    impl = _image_directory_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to materialize"),
    }
    | IMAGE_TOOLS_ATTR,
)

def image_archive(name: str, distribution: str | None = None, visibility: list[str] | None = None, **kwargs) -> None:
    """Declare one, compatible with the distributions its package serves."""
    _image_archive(name = name, **(distributed(name, distribution, visibility) | kwargs))

def image_directory(name: str, distribution: str | None = None, visibility: list[str] | None = None, **kwargs) -> None:
    """Declare one, compatible with the distributions its package serves."""
    _image_directory(name = name, **(distributed(name, distribution, visibility) | kwargs))
