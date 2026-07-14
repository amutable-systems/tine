"""Archive and materialized-directory image outputs."""

load("//image:layer.bzl", "ImageInfo")
load(":actions.bzl", "archive_action")

_EXT = {"tar": "tar", "cpio": "cpio"}

DirectoryImageInfo = provider(
    doc = "A materialized directory view of a logical image.",
    fields = {
        "engine": provider_field(Dependency),
        "rootfs": provider_field(Artifact),
        "source": provider_field(Dependency),
    },
)

CpioArchiveInfo = provider(
    doc = "An uncompressed newc CPIO archive.",
    fields = {
        "archive": provider_field(Artifact),
        "source": provider_field(Dependency),
    },
)

def _image_archive_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    out = ctx.actions.declare_output("image." + _EXT[ctx.attrs.format])
    archive_action(
        ctx,
        image.engine,
        image.layers,
        image.tmpfiles,
        ctx.attrs._driver,
        ctx.attrs.format,
        out.as_output(),
    )
    providers = [DefaultInfo(default_output = out)]
    if ctx.attrs.format == "cpio":
        providers.append(CpioArchiveInfo(archive = out, source = ctx.attrs.image))
    return providers

image_archive = rule(
    impl = _image_archive_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to archive"),
        "format": attrs.enum(["tar", "cpio"], default = "tar"),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:archive"),
    },
)

def _image_directory_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    out = ctx.actions.declare_output("image.rootfs", dir = True)
    archive_action(
        ctx,
        image.engine,
        image.layers,
        image.tmpfiles,
        ctx.attrs._driver,
        "directory",
        out.as_output(),
    )
    return [
        DefaultInfo(default_output = out),
        DirectoryImageInfo(engine = image.engine, rootfs = out, source = ctx.attrs.image),
    ]

image_directory = rule(
    impl = _image_directory_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to materialize"),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:archive"),
    },
)
