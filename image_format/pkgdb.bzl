"""Capture an image's package database as a separate artifact.

This is a supply-chain output, not shipped in the image. The rule mounts the image's delta
stack with a disposable overlay upper (like `image_archive`/`image_directory`) and writes the
trimmed database. Compositions expose it as an explicit `<name>.pkgdb` sibling target.
"""

load("//image:actions.bzl", "terminal_image_command")
load("//image:layer.bzl", "ImageInfo")

def _image_pkgdb_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    out = ctx.actions.declare_output("rpmdb.sqlite")
    cmd = terminal_image_command(image, ctx.attrs._driver)
    cmd.add("--out", out.as_output())
    ctx.actions.run(cmd, category = "image_pkgdb")
    return [DefaultInfo(default_output = out)]

image_pkgdb = rule(
    impl = _image_pkgdb_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to capture from"),
        # Only rpm has a driver so far; it belongs to the package system rather than here.
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:rpmdb"),
    },
)
