"""Raw disk image outputs."""

load("//engine:rules.bzl", "EngineInfo", "chroot_run")
load("//image:layer.bzl", "ImageInfo")

DiskImageInfo = provider(
    doc = "A raw disk image suitable for a VM runner or later disk-format conversion.",
    fields = {
        "engine": provider_field(Dependency),
        "image": provider_field(Artifact),
    },
)

def _image_disk_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    out = ctx.actions.declare_output("image.raw")
    cmd = cmd_args(
        chroot_run(engine = image.engine[EngineInfo], exe = ctx.attrs._driver),
        "--out",
        out.as_output(),
        "--identity",
        str(ctx.label),
    )
    if ctx.attrs.seed != None:
        cmd.add("--seed", ctx.attrs.seed)
    for lower in image.layers:
        cmd.add("--lower", lower)
    for snippet in image.tmpfiles:
        cmd.add("--tmpfiles", snippet)
    for name, definition in ctx.attrs.definitions.items():
        cmd.add("--definition", name, definition)
    ctx.actions.run(cmd, category = "image_disk")
    return [
        DefaultInfo(default_output = out),
        DiskImageInfo(engine = image.engine, image = out),
    ]

image_disk = rule(
    impl = _image_disk_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to write into the disk"),
        "seed": attrs.option(
            attrs.string(),
            default = None,
            doc = "explicit GPT/partition UUID seed; by default derive one from target identity and definitions",
        ),
        "definitions": attrs.dict(
            key = attrs.string(),
            value = attrs.source(),
            default = {},
            doc = "repart.d filename-to-source mapping replacing the builtin ESP+root pair",
        ),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:disk"),
    },
)
