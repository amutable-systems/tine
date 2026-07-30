"""Semantic boot-artifact extraction from a completed logical image."""

load(
    "//image:image.bzl",
    "IMAGE_TOOLS_ATTR",
    "ImageInfo",
    "ImageToolsInfo",
    "terminal_image_command",
)

_ARTIFACTS = {
    "initrd": "initrd",
    "kernel": "vmlinuz",
    "uki": "image.efi",
}

def _bootable_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    tools = ctx.attrs._tools[ImageToolsInfo]
    selection = ctx.actions.declare_output("boot-artifacts.json")
    select = terminal_image_command(
        ctx,
        driver = "boot",
        exe = tools.boot,
        image = image,
        spec = {"out": selection.as_output()},
    )
    ctx.actions.run(select, category = "boot_artifact_select")

    artifacts = {}
    for kind in sorted(_ARTIFACTS):
        out = ctx.actions.declare_output(_ARTIFACTS[kind])
        extract = terminal_image_command(
            ctx,
            # The extracted artifacts are named after their kind, so scope the spec by name
            # rather than by identifier, whose directory would collide with them.
            driver = "artifacts-" + kind,
            exe = tools.artifacts,
            image = image,
            spec = {
                "artifact": kind,
                "manifest": selection,
                "out": out.as_output(),
            },
        )
        ctx.actions.run(extract, category = "boot_artifact_" + kind)
        artifacts[kind] = out

    # A UKI is not guaranteed to exist (kernels can ship as plain files), so the default
    # outputs stay limited to the artifacts selection always yields; [uki] extracts on demand.
    return [
        DefaultInfo(
            default_outputs = [artifacts["kernel"], artifacts["initrd"]],
            sub_targets = {kind: [DefaultInfo(default_output = artifacts[kind])] for kind in artifacts},
        ),
    ]

bootable = rule(
    impl = _bootable_impl,
    attrs = {
        "image": attrs.dep(
            providers = [ImageInfo],
            doc = "the completed logical image from which to select boot artifacts",
        ),
    }
    | IMAGE_TOOLS_ATTR,
)
