"""Generate SPDX and CycloneDX SBOMs from a logical image with syft.

This is a supply-chain output, not shipped in the image. The rule mounts the image's delta
stack with a disposable overlay upper (like `image_archive`/`image_directory`) and writes both
SBOMs from a single scan. Compositions expose them as an explicit `<name>.sbom` sibling target.
"""

load("//image:actions.bzl", "terminal_image_command")
load("//image:layer.bzl", "ImageInfo")

def _image_sbom_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    spdx = ctx.actions.declare_output("sbom.spdx.json")
    cdx = ctx.actions.declare_output("sbom.cdx.json")
    cmd = terminal_image_command(image, ctx.attrs._driver)
    cmd.add("--syft", ctx.attrs._syft[DefaultInfo].default_outputs[0])
    cmd.add("--source-name", ctx.attrs.source_name or ctx.label.name)
    cmd.add("--source-version", ctx.attrs.version)
    cmd.add("--spdx", spdx.as_output())
    cmd.add("--cdx", cdx.as_output())
    ctx.actions.run(cmd, category = "image_sbom")

    # One syft run emits both formats; there is no per-format selection.
    return [DefaultInfo(default_outputs = [spdx, cdx])]

image_sbom = rule(
    impl = _image_sbom_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to scan"),
        "source_name": attrs.option(attrs.string(), default = None, doc = "SBOM source name; defaults to the target name"),
        "version": attrs.string(default = "0", doc = "SBOM source version"),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:sbom"),
        "_syft": attrs.dep(providers = [RunInfo], default = "tine//tools:syft"),
    },
)
