"""Aggregate independent capabilities of one logical image."""

load("//image_format:archive.bzl", "DirectoryImageInfo")
load("//image_format:disk.bzl", "DiskImageInfo", "RootHashInfo")
load("//image_format:rpmdb.bzl", "RpmdbInfo")
load(":boot.bzl", "BootableImageInfo")
load(":layer.bzl", "ImageInfo")

def _check_source(name: str, source: Dependency, image: Dependency) -> None:
    if source.label != image.label:
        fail("image result {} facet comes from {}, expected {}".format(name, source.label, image.label))

def _image_result_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image
    providers = [image[ImageInfo]]
    sub_targets = {}
    facets = {}

    if ctx.attrs.bootable != None:
        dep = ctx.attrs.bootable
        info = dep[BootableImageInfo]
        _check_source("bootable", info.source, image)
        providers.append(info)
        sub_targets["bootable"] = [dep[DefaultInfo], info]
        facets["bootable"] = dep

    if ctx.attrs.directory != None:
        dep = ctx.attrs.directory
        info = dep[DirectoryImageInfo]
        _check_source("directory", info.source, image)
        providers.append(info)
        sub_targets["directory"] = [dep[DefaultInfo], info]
        facets["directory"] = dep

    if ctx.attrs.disk != None:
        dep = ctx.attrs.disk
        info = dep[DiskImageInfo]
        _check_source("disk", info.source, image)
        providers.append(info)
        disk_subtarget = [dep[DefaultInfo], info]
        root_hash = dep.get(RootHashInfo)
        if root_hash != None:
            providers.append(root_hash)
            disk_subtarget.append(root_hash)
        sub_targets["disk"] = disk_subtarget
        facets["disk"] = dep

    # Supply-chain outputs are never the default facet, but ride along on a normal build via
    # other_outputs (and stay individually reachable as subtargets).
    ride_along = []
    if ctx.attrs.rpmdb != None:
        dep = ctx.attrs.rpmdb
        info = dep[RpmdbInfo]
        _check_source("rpmdb", info.source, image)
        providers.append(info)
        sub_targets["rpmdb"] = [dep[DefaultInfo], info]
        ride_along.extend(dep[DefaultInfo].default_outputs)

    default = facets.get(ctx.attrs.default_facet)
    if default == None:
        fail("image result default facet {!r} is not configured".format(ctx.attrs.default_facet))
    info = default[DefaultInfo]
    return [
        DefaultInfo(
            default_outputs = info.default_outputs,
            other_outputs = info.other_outputs + ride_along,
            sub_targets = sub_targets,
        ),
    ] + providers

image_result = rule(
    impl = _image_result_impl,
    attrs = {
        "bootable": attrs.option(attrs.dep(providers = [BootableImageInfo]), default = None),
        "default_facet": attrs.enum(["bootable", "directory", "disk"]),
        "directory": attrs.option(attrs.dep(providers = [DirectoryImageInfo]), default = None),
        "disk": attrs.option(attrs.dep(providers = [DiskImageInfo]), default = None),
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image shared by every facet"),
        "rpmdb": attrs.option(attrs.dep(providers = [RpmdbInfo]), default = None),
    },
)
