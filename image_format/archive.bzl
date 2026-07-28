"""Archive and materialized-directory image outputs."""

load("//image:layer.bzl", "ImageInfo")
load(":actions.bzl", "archive_action")
load(":rpmdb.bzl", "RpmdbInfo")
load(":sbom.bzl", "SbomInfo")

_EXT = {"tar": "tar", "cpio": "cpio"}
COMPRESSIONS = ["none", "zstd"]
_COMPRESSION_EXT = {"none": "", "zstd": ".zst"}

DirectoryImageInfo = provider(
    doc = "A materialized directory view of a logical image.",
    fields = {
        "engine": provider_field(Dependency),
        "rootfs": provider_field(Artifact),
        "source": provider_field(Dependency),
    },
)

CpioArchiveInfo = provider(
    doc = "A newc CPIO archive, compressed as declared.",
    fields = {
        "archive": provider_field(Artifact),
        "source": provider_field(Dependency),
    },
)

def _image_archive_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    out = ctx.actions.declare_output(
        "image." + _EXT[ctx.attrs.format] + _COMPRESSION_EXT[ctx.attrs.compression],
    )
    archive_action(
        ctx,
        image.engine,
        image.layers,
        image.tmpfiles,
        ctx.attrs._driver,
        ctx.attrs.format,
        out.as_output(),
        ctx.attrs.compression,
    )

    # Supply-chain sidecars ride along on a normal build via other_outputs and stay reachable as
    # subtargets. They are separate targets (one action each, shared with a standalone build), so
    # this only forwards their outputs; neither is shipped in the archive.
    providers = []
    sidecars = []
    sub_targets = {}

    if ctx.attrs.rpmdb != None:
        info = ctx.attrs.rpmdb[DefaultInfo]
        sidecars.extend(info.default_outputs)
        sub_targets["rpmdb"] = [info]
        providers.append(ctx.attrs.rpmdb[RpmdbInfo])

    if ctx.attrs.sbom != None:
        info = ctx.attrs.sbom[DefaultInfo]
        sidecars.extend(info.default_outputs)
        sub_targets["sbom"] = [info]
        providers.append(ctx.attrs.sbom[SbomInfo])

    providers.insert(0, DefaultInfo(default_output = out, other_outputs = sidecars, sub_targets = sub_targets))
    if ctx.attrs.format == "cpio":
        providers.append(CpioArchiveInfo(archive = out, source = ctx.attrs.image))
    return providers

image_archive = rule(
    impl = _image_archive_impl,
    attrs = {
        "compression": attrs.enum(COMPRESSIONS, default = "none"),
        "format": attrs.enum(["tar", "cpio"], default = "tar"),
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to archive"),
        "rpmdb": attrs.option(attrs.dep(providers = [RpmdbInfo]), default = None, doc = "rpmdb artifact to ride along"),
        "sbom": attrs.option(attrs.dep(providers = [SbomInfo]), default = None, doc = "SBOM artifacts to ride along"),
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
