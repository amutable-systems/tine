"""Capture an image's rpm database as a separate artifact.

This is a supply-chain output, not shipped in the image. The rule mounts the image's delta
stack with a disposable overlay upper (like `image_archive`/`image_directory`) and writes the
trimmed database. `image_archive`/`rootfs_archive` can fold it into a normal build.
"""

load("//image:actions.bzl", "terminal_image_command")
load("//image:layer.bzl", "ImageInfo")

RpmdbInfo = provider(
    doc = "A trimmed, Packages-only copy of an image's rpm database.",
    fields = {
        "rpmdb": provider_field(Artifact),
        "source": provider_field(Dependency),
    },
)

def _image_rpmdb_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    out = ctx.actions.declare_output("rpmdb.sqlite")
    cmd = terminal_image_command(image, ctx.attrs._driver)
    cmd.add("--out", out.as_output())
    ctx.actions.run(cmd, category = "image_rpmdb")
    return [
        DefaultInfo(default_output = out),
        RpmdbInfo(rpmdb = out, source = ctx.attrs.image),
    ]

image_rpmdb = rule(
    impl = _image_rpmdb_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to capture from"),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:rpmdb"),
    },
)
