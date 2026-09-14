"""What a build publishes, under the names it publishes them as.

A published name is a contract with whatever consumes the build: systemd-sysupdate matches its
transfer sources against them, so the name carries the image identity, the version, and, for a
partition, the type and UUID repart gave it. Each rule names what it produces, and a rule that
gathers artifacts from several targets reads those names here rather than composing any of its own.

A partition is the exception: its name exists only once repart has run, so it travels as the typed
result of the run rather than as a name, and whatever materializes it reads the name back out of
the metadata written beside it. That is the whole reason `image_artifacts` assembles its directory
from a dynamic action: everything else in it is known while the graph is still being built.

A release also says what each file is. `artifacts_<arch>.json` lists every published file with
its kind and image, so a consumer reads roles rather than names. An `author` given to
`image_artifacts` reads that listing and the files and adds files of its own. That is how a
project signs or describes a release in a format tine knows nothing about.
"""

load("//:specs.bzl", "spec_args")
load("//image:image.bzl", "ARCHES", "ImageInfo", "image_metadata_subtargets")
load("//image_format:disk.bzl", "PartitionInfo", "RepartInfo")

PublishedInfo = provider(
    doc = "The artifacts one target contributes to a release, keyed by published name.",
    fields = {
        "artifacts": provider_field(dict[str, Artifact]),
        # This is the image the artifacts and partitions belong to. It is the basename they are
        # published under.
        "image": provider_field(str, default = ""),
        # This maps each published name to the artifact's kind (see docs/images.md). A partition's
        # kind is its type without an explicit architecture.
        "kinds": provider_field(dict[str, str], default = {}),
        "partitions": provider_field(list[PartitionInfo], default = []),
    },
)

def published_metadata_subtargets(image: ImageInfo, basename: str) -> dict[str, list[Provider]]:
    """The metadata views of an image, publishing themselves under the name it is published as.

    Each view publishes itself rather than travelling in the image's own set, exactly as a re-encoding
    does, so a release builds the metadata it lists and nothing else.
    """
    sub_targets = image_metadata_subtargets(image)
    cyclonedx = "{}.cdx.json".format(basename)
    spdx = "{}.spdx.json".format(basename)
    sub_targets["sbom"] += [
        PublishedInfo(
            artifacts = {cyclonedx: image.sbom.cyclonedx, spdx: image.sbom.spdx},
            image = basename,
            kinds = {cyclonedx: "sbom", spdx: "sbom"},
        ),
    ]
    if image.pkgdb != None:
        # The capture carries the format in its name, so a release says what it hands out.
        pkgdb = "{}.{}".format(basename, image.pkgdb.basename)
        sub_targets["pkgdb"] += [
            PublishedInfo(artifacts = {pkgdb: image.pkgdb}, image = basename, kinds = {pkgdb: "pkgdb"}),
        ]
    return sub_targets

def _partition_kind(type: str, arch: str) -> str:
    """Return the kind of a partition, which is its repart type without the release's architecture.

    An arch-explicit type such as usr-x86-64-verity thus has the kind usr-verity.
    """
    for base in ("root", "usr"):
        prefix = "{}-{}".format(base, arch)
        if type == prefix or type.startswith(prefix + "-"):
            return base + type.removeprefix(prefix)
    return type

# This holds what the listing needs that is known before the partitions are named. It has the kind
# and image by published name and by partition index, and the driver that writes the listing.
Gathering = record(
    arch = field(str),
    images = field(dict[str, str]),
    kinds = field(dict[str, str]),
    partition_images = field(list[str]),
    partition_kinds = field(list[str]),
    publish = field(RunInfo),
    # This maps each image to its disk's usr verity root hash.
    root_hashes = field(dict[str, Artifact]),
)

# This describes the author of a release's own files. It holds the author's command, the names it
# writes, and how it runs.
Authoring = record(
    args = field(list[typing.Any]),
    execution = field(dict[str, typing.Any]),
    outputs = field(list[str]),
    run = field(RunInfo),
)

def _assemble_impl(
    actions: AnalysisActions,
    author: Authoring | None,
    blocks: list[Artifact],
    directory: OutputArtifact,
    gathering: Gathering,
    metadata: list[ArtifactValue],
    tree: dict[str, Artifact],
) -> list[Provider]:
    published = dict(tree)
    kinds = dict(gathering.kinds)
    images = dict(gathering.images)
    for index, value in enumerate(metadata):
        name = value.read_json()["published"]
        if name in published:
            fail("image_artifacts: {} is published by more than one target".format(name))
        published[name] = blocks[index]
        kinds[name] = gathering.partition_kinds[index]
        images[name] = gathering.partition_images[index]

    # The listing is written by a driver rather than as JSON here. A DDI's verity root hash is the
    # content of a file, and only an action can read it.
    listing_name = "artifacts_{}.json".format(gathering.arch)
    listing = actions.declare_output(listing_name)
    actions.run(
        cmd_args(
            gathering.publish,
            spec_args(
                actions,
                "publish.spec.json",
                {
                    "arch": gathering.arch,
                    "files": {name: {"image": images[name], "kind": kinds[name], "path": published[name]} for name in published},
                    "out": listing.as_output(),
                    "root_hashes": gathering.root_hashes,
                },
            ),
        ),
        category = "publish_listing",
    )
    release = published | {listing_name: listing}

    if author != None:
        # The author sees the files under their published names and writes into a directory of
        # its own, from which the release links only the names in author_outputs.
        files = actions.symlinked_dir("files", published)
        authored = actions.declare_output("authored", dir = True)
        actions.run(
            cmd_args(
                author.run,
                "--artifacts",
                listing,
                "--files",
                files,
                "--out-dir",
                authored.as_output(),
                author.args,
            ),
            category = "author",
            **author.execution,
        )
        for name in author.outputs:
            if name in release:
                fail("image_artifacts: author output {} clashes with a published name".format(name))
            release[name] = authored.project(name)

    # Symlinks, not copies: a release is mostly disk images, and every one of them already exists.
    actions.symlinked_dir(directory, release)
    return []

_assemble = dynamic_actions(
    impl = _assemble_impl,
    attrs = {
        "author": dynattrs.option(dynattrs.value(Authoring)),
        "blocks": dynattrs.list(dynattrs.value(Artifact)),
        "directory": dynattrs.output(),
        "gathering": dynattrs.value(Gathering),
        "metadata": dynattrs.list(dynattrs.artifact_value()),
        "tree": dynattrs.dict(str, dynattrs.value(Artifact)),
    },
)

def _check_plain_name(what: str, name: str) -> str:
    if "/" in name or name.startswith(".") or name == "":
        fail("image_artifacts: {} must be a plain file name, got {!r}".format(what, name))
    return name

def _authoring(ctx: AnalysisContext) -> Authoring | None:
    if ctx.attrs.author == None:
        for what in ("author_args", "author_local_only", "author_outputs"):
            if getattr(ctx.attrs, what):
                fail("image_artifacts: {} needs author".format(what))
        return None
    if not ctx.attrs.author_outputs:
        fail("image_artifacts: author needs author_outputs")
    for index, name in enumerate(ctx.attrs.author_outputs):
        _check_plain_name("author output", name)
        if name in ctx.attrs.author_outputs[:index]:
            fail("image_artifacts: author output {} is listed twice".format(name))
    execution = {}
    if ctx.attrs.author_local_only:
        # As with an external signing key, what the author reads from the host is no action input.
        execution = {"allow_cache_upload": False, "local_only": True}
    return Authoring(
        args = ctx.attrs.author_args,
        execution = execution,
        outputs = ctx.attrs.author_outputs,
        run = ctx.attrs.author[RunInfo],
    )

def _image_artifacts_impl(ctx: AnalysisContext) -> list[Provider]:
    if not ctx.attrs.targets:
        fail("image_artifacts: at least one target is required")

    tree = {}
    kinds = {}
    images = {}
    blocks = []
    metadata = []
    partition_images = []
    partition_kinds = []
    root_hashes = {}
    arch = ARCHES[ctx.attrs.arch].systemd
    for dep in ctx.attrs.targets:
        published = dep[PublishedInfo]
        if published.image == "" and (published.artifacts or published.partitions):
            fail("image_artifacts: {} publishes without an image".format(dep.label))
        for name, artifact in published.artifacts.items():
            if name in tree:
                fail("image_artifacts: {} is published by more than one target".format(name))
            if name not in published.kinds:
                fail("image_artifacts: {} publishes {} without a kind".format(dep.label, name))
            tree[name] = artifact
            kinds[name] = published.kinds[name]
            images[name] = published.image
        for partition in published.partitions:
            blocks.append(partition.blocks)
            metadata.append(partition.metadata)
            partition_images.append(published.image)
            partition_kinds.append(_partition_kind(partition.definition["type"], arch))

        # The listing records a disk's verity root hash on the usr partition it protects. A root
        # partition's hash is not listed.
        repart = dep.get(RepartInfo)
        root_hash = repart.root_hash if repart != None else None
        if published.partitions and root_hash != None and root_hash.kind == "usr":
            root_hashes[published.image] = root_hash.hash

    directory = ctx.actions.declare_output("artifacts", dir = True)
    ctx.actions.dynamic_output_new(
        _assemble(
            author = _authoring(ctx),
            blocks = blocks,
            directory = directory.as_output(),
            gathering = Gathering(
                arch = arch,
                images = images,
                kinds = kinds,
                partition_images = partition_images,
                partition_kinds = partition_kinds,
                publish = ctx.attrs._publish[RunInfo],
                root_hashes = root_hashes,
            ),
            metadata = metadata,
            tree = tree,
        ),
    )
    return [DefaultInfo(default_output = directory)]

image_artifacts = rule(
    impl = _image_artifacts_impl,
    attrs = {
        "arch": attrs.enum(ARCHES.keys(), default = "x86_64", doc = "The architecture the release is for. It names the listing."),
        "author": attrs.option(
            attrs.dep(providers = [RunInfo]),
            default = None,
            doc = "This adds files to the release. It runs as <author> --artifacts <listing> --files <dir> --out-dir <dir> <author_args>.",
        ),
        "author_args": attrs.list(attrs.arg(), default = [], doc = "These arguments are appended to the author's command."),
        "author_local_only": attrs.bool(
            default = False,
            doc = "This runs the author on this host and never caches it. It is for an author that reads host state such as a signing key.",
        ),
        "author_outputs": attrs.list(attrs.string(), default = [], doc = "These are the file names the author writes to --out-dir. Each one joins the release."),
        "targets": attrs.list(
            attrs.dep(providers = [PublishedInfo]),
            doc = "the targets whose published artifacts make up the release",
        ),
        "_publish": attrs.exec_dep(providers = [RunInfo], default = "tine//image:publish"),
    },
)
