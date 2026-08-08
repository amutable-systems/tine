"""Semantic boot-artifact extraction from a completed logical image."""

load(
    "//image:image.bzl",
    "IMAGE_TOOLS_ATTR",
    "ImageInfo",
    "ImageToolsInfo",
    "declare_out",
    "terminal_image_command",
)

_ARTIFACTS = {
    "initrd": "initrd",
    "kernel": "vmlinuz",
    "uki": "image.efi",
}

BootArtifactsInfo = record(
    initrd = Artifact,
    kernel = Artifact,
    uki = Artifact,
)

def declare_boot_artifacts(
    ctx: AnalysisContext,
    *,
    image: ImageInfo,
    identifier: str | None = None,
) -> BootArtifactsInfo:
    """Declare extraction of what a completed image boots: its kernel, initrd and UKI.

    The driver picks the newest kernel the image carries and reads the initrd and kernel out of
    the UKI's PE sections when it boots one, so the results are what a direct kernel boot needs.
    """
    tools = ctx.attrs._tools[ImageToolsInfo]
    selection = declare_out(ctx, identifier, "boot-artifacts.json")
    select = terminal_image_command(
        ctx,
        driver = "boot",
        exe = tools.boot,
        identifier = identifier,
        image = image,
        spec = {"out": selection.as_output()},
    )
    ctx.actions.run(select, category = "boot_artifact_select", identifier = identifier)

    artifacts = {}
    for kind in sorted(_ARTIFACTS):
        out = declare_out(ctx, identifier, _ARTIFACTS[kind])
        extract = terminal_image_command(
            ctx,
            # The extracted artifacts are named after their kind, so the specs of one composition
            # step are told apart by driver name; scoping them by kind would name a directory
            # after a file already declared beside it.
            driver = "artifacts-" + kind,
            exe = tools.artifacts,
            identifier = identifier,
            image = image,
            spec = {
                "artifact": kind,
                "manifest": selection,
                "out": out.as_output(),
            },
        )
        ctx.actions.run(extract, category = "boot_artifact_" + kind, identifier = identifier)
        artifacts[kind] = out
    return BootArtifactsInfo(
        initrd = artifacts["initrd"],
        kernel = artifacts["kernel"],
        uki = artifacts["uki"],
    )

def boot_subtargets(artifacts: BootArtifactsInfo) -> dict[str, list[Provider]]:
    """The extracted artifacts, exposed wherever the image they come from is."""
    return {
        "initrd": [DefaultInfo(default_output = artifacts.initrd)],
        "kernel": [DefaultInfo(default_output = artifacts.kernel)],
        "uki": [DefaultInfo(default_output = artifacts.uki)],
    }

def _bootable_impl(ctx: AnalysisContext) -> list[Provider]:
    artifacts = declare_boot_artifacts(ctx, image = ctx.attrs.image[ImageInfo])

    # A UKI is not guaranteed to exist (kernels can ship as plain files), so the default
    # outputs stay limited to the artifacts selection always yields; [uki] extracts on demand.
    return [
        DefaultInfo(
            default_outputs = [artifacts.kernel, artifacts.initrd],
            sub_targets = boot_subtargets(artifacts),
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
