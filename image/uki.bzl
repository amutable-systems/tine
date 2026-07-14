"""Unified kernel image artifacts."""

load("//engine:rules.bzl", "EngineInfo", "chroot_run")
load(":layer.bzl", "ImageInfo")

def _uki_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    out = ctx.actions.declare_output("image.efi")
    cmd = cmd_args(
        chroot_run(engine = image.engine[EngineInfo], exe = ctx.attrs._driver),
        "--out",
        out.as_output(),
        "--cmdline",
        ctx.attrs.cmdline,
        "--arch",
        ctx.attrs.arch,
    )
    if ctx.attrs.kernel_version != None:
        cmd.add("--kernel-version", ctx.attrs.kernel_version)
    for lower in image.layers:
        cmd.add("--lower", lower)
    for initrd in ctx.attrs.initrds:
        cmd.add("--initrd", initrd)
    ctx.actions.run(cmd, category = "uki")
    return [DefaultInfo(default_output = out)]

uki = rule(
    impl = _uki_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the image supplying the kernel, modules, stub, and os-release"),
        "initrds": attrs.list(attrs.source(), default = [], doc = "base initrd archives in load order"),
        "cmdline": attrs.string(default = "", doc = "kernel command line embedded in the UKI"),
        "arch": attrs.enum(["x86_64"], default = "x86_64"),
        "kernel_version": attrs.option(
            attrs.string(),
            default = None,
            doc = "kernel release to use; by default require exactly one installed kernel",
        ),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image:uki"),
    },
)
