"""Capture an image's package database as a separate artifact.

This is a supply-chain output, not shipped in the image. The rule mounts the image's delta
stack with a disposable overlay upper (like `image_archive`/`image_directory`) and writes the
trimmed database. Compositions expose it as an explicit `<name>.pkgdb` sibling target.
"""

load("//image:actions.bzl", "terminal_image_command")
load("//image:layer.bzl", "ImageInfo")
load("//package:manager.bzl", "PackageManagerInfo")
load("//package:system.bzl", "PackageSystemInfo")

def _image_pkgdb_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    if image.package_manager == None:
        fail("image_pkgdb: {} installs no packages, so it has no database".format(ctx.attrs.image.label))
    system = image.package_manager[PackageManagerInfo].package_system[PackageSystemInfo]

    # A package database is not one file everywhere: rpm keeps a single SQLite database, dpkg a
    # status file plus per-package lists. The driver fills a directory with whatever its own is.
    out = ctx.actions.declare_output("pkgdb", dir = True)
    cmd = terminal_image_command(image, system.pkgdb)
    cmd.add("--out", out.as_output())
    ctx.actions.run(cmd, category = "image_pkgdb")
    return [DefaultInfo(default_output = out)]

image_pkgdb = rule(
    impl = _image_pkgdb_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to capture from"),
    },
)
