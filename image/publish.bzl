"""What a build publishes, under the names it publishes them as.

A published name is a contract with whatever consumes the build: systemd-sysupdate matches its
transfer sources against them, so the name carries the image identity, the version, and, for a
partition, the type and UUID repart gave it. Each rule names what it produces, and a rule that
gathers artifacts from several targets reads those names here rather than composing any of its own.

A partition is the exception: its name exists only once repart has run, so it travels as the typed
result of the run rather than as a name, and whatever materializes it reads the name back out of
the metadata written beside it. That is the whole reason `image_artifacts` assembles its directory
from a dynamic action: everything else in it is known while the graph is still being built.
"""

load("//image:image.bzl", "ImageInfo", "image_metadata_subtargets")
load("//image_format:disk.bzl", "PartitionInfo")

PublishedInfo = provider(
    doc = "The artifacts one target contributes to a release, keyed by published name.",
    fields = {
        "artifacts": provider_field(dict[str, Artifact]),
        "partitions": provider_field(list[PartitionInfo], default = []),
    },
)

def published_metadata_subtargets(image: ImageInfo, basename: str) -> dict[str, list[Provider]]:
    """The metadata views of an image, publishing themselves under the name it is published as.

    Each view publishes itself rather than travelling in the image's own set, exactly as a re-encoding
    does, so a release builds the metadata it lists and nothing else.
    """
    sub_targets = image_metadata_subtargets(image)
    sub_targets["sbom"] += [
        PublishedInfo(
            artifacts = {
                "{}.cdx.json".format(basename): image.sbom.cyclonedx,
                "{}.spdx.json".format(basename): image.sbom.spdx,
            },
        ),
    ]
    if image.pkgdb != None:
        # The capture carries the format in its name, so a release says what it hands out.
        sub_targets["pkgdb"] += [
            PublishedInfo(artifacts = {"{}.{}".format(basename, image.pkgdb.basename): image.pkgdb}),
        ]
    return sub_targets

def _assemble_impl(
    actions: AnalysisActions,
    blocks: list[Artifact],
    directory: OutputArtifact,
    metadata: list[ArtifactValue],
    tree: dict[str, Artifact],
) -> list[Provider]:
    published = dict(tree)
    for index, value in enumerate(metadata):
        name = value.read_json()["published"]
        if name in published:
            fail("image_artifacts: {} is published by more than one target".format(name))
        published[name] = blocks[index]

    # Symlinks, not copies: a release is mostly disk images, and every one of them already exists.
    actions.symlinked_dir(directory, published)
    return []

_assemble = dynamic_actions(
    impl = _assemble_impl,
    attrs = {
        "blocks": dynattrs.list(dynattrs.value(Artifact)),
        "directory": dynattrs.output(),
        "metadata": dynattrs.list(dynattrs.artifact_value()),
        "tree": dynattrs.dict(str, dynattrs.value(Artifact)),
    },
)

def _image_artifacts_impl(ctx: AnalysisContext) -> list[Provider]:
    if not ctx.attrs.targets:
        fail("image_artifacts: at least one target is required")

    tree = {}
    blocks = []
    metadata = []
    for dep in ctx.attrs.targets:
        published = dep[PublishedInfo]
        for name, artifact in published.artifacts.items():
            if name in tree:
                fail("image_artifacts: {} is published by more than one target".format(name))
            tree[name] = artifact
        for partition in published.partitions:
            blocks.append(partition.blocks)
            metadata.append(partition.metadata)

    directory = ctx.actions.declare_output("artifacts", dir = True)
    ctx.actions.dynamic_output_new(
        _assemble(
            blocks = blocks,
            directory = directory.as_output(),
            metadata = metadata,
            tree = tree,
        )
    )
    return [DefaultInfo(default_output = directory)]

image_artifacts = rule(
    impl = _image_artifacts_impl,
    attrs = {
        "targets": attrs.list(
            attrs.dep(providers = [PublishedInfo]),
            doc = "the targets whose published artifacts make up the release",
        ),
    },
)
