# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""What a build publishes, under the names it publishes them as.

A published name is a contract with whatever consumes the build: systemd-sysupdate matches its
transfer sources against them, so the name carries the image identity, the version, and, for a
partition, the type and UUID repart gave it. Each rule names what it produces, and a rule that
gathers artifacts from several targets reads those names here rather than composing any of its own.
The release's own description and the files its author hook writes are the only names it adds.

A partition is the exception: its name exists only once repart has run, so it travels as the typed
result of the run rather than as a name, and whatever materializes it reads the name back out of
the metadata written beside it. That is the whole reason `image_artifacts` assembles its directory
from a dynamic action: everything else in it is known while the graph is still being built.

The release also carries `artifacts_<arch>.json`, which describes each published file (see
docs/user/images.md), so a consumer can read roles rather than match on a naming scheme.
An `author_hook` given to `image_artifacts` can add files of its own, such as update metadata (see
docs/user/images.md).
"""

load("//distribution:defs.bzl", "distribution")
load("//package:manager.bzl", "PackageManagerInfo")
load("//package:system.bzl", "PackageSystemInfo")
load("//platforms:architecture.bzl", "architecture")
load(":ddi.bzl", "DdiInfo", "PartitionInfo")
load(":image.bzl", "FILENAME_PATTERN", "ImageInfo", "check_name", "image_metadata_subtargets")

PublishedFile = record(
    artifact = field(Artifact),
    kind = field(str),
    subkind = field(str | None, default = None),
)

PublishedInfo = provider(
    doc = "The artifacts one target contributes to a release, keyed by published name.",
    fields = {
        "artifacts": provider_field(dict[str, PublishedFile]),
        "image": provider_field(str),
        "partitions": provider_field(list[PartitionInfo], default = []),
    },
)

def published_metadata_subtargets(image: ImageInfo, basename: str, image_name: str | None = None) -> dict[str, list[Provider]]:
    """The metadata views of an image, publishing themselves under the name it is published as.

    Each view publishes itself rather than travelling in the image's own set, exactly as a re-encoding
    does, so a release builds the metadata it lists and nothing else. `image_name` is the image the
    views belong to in the release's description, `basename` by default.
    """
    image_name = image_name or basename
    sub_targets = image_metadata_subtargets(image)
    sub_targets["sbom"] += [
        PublishedInfo(
            artifacts = {
                "{}.cdx.json".format(basename): PublishedFile(artifact = image.sbom.cyclonedx, kind = "sbom", subkind = "cyclonedx"),
                "{}.spdx.json".format(basename): PublishedFile(artifact = image.sbom.spdx, kind = "sbom", subkind = "spdx"),
            },
            image = image_name,
        ),
    ]
    if image.pkgdb != None:
        # The arch schema of a package system is named after the system, rpm, deb or pacman.
        system = image.package_manager[PackageManagerInfo].package_system[PackageSystemInfo].arch_schema

        # The capture carries the format in its name, so a release says what it hands out.
        sub_targets["pkgdb"] += [
            PublishedInfo(
                artifacts = {"{}.{}".format(basename, image.pkgdb.basename): PublishedFile(artifact = image.pkgdb, kind = "pkgdb", subkind = system)},
                image = image_name,
            ),
        ]
    return sub_targets

_Described = record(
    artifact = field(Artifact),
    # A file of the release as a whole, such as an author hook's output, belongs to no image.
    image = field(str | None),
    kind = field(str),
    subkind = field(str | None),
    # This indexes the verity root hash of the file in the release's root hash files.
    root_hash = field(int | None),
)

# The author hook runs in the dynamic action, because it reads the whole release, whose partitions
# are named only there.
_AuthorHook = record(
    # This maps each output to its kind and optional subkind.
    outputs = field(dict[str, dict[str, str]]),
    run = field(RunInfo),
)

def _write_description(actions: AnalysisActions, path: str, arch: str, files: dict[str, _Described], hashes: list[str]) -> Artifact:
    entries = {}
    for name in sorted(files):
        file = files[name]
        entry = {}
        if file.image != None:
            entry["image"] = file.image
        entry["kind"] = file.kind
        if file.root_hash != None:
            entry["root_hash"] = hashes[file.root_hash]
        if file.subkind != None:
            entry["subkind"] = file.subkind
        entries[name] = entry
    description = actions.declare_output(path)
    actions.write_json(description, {"arch": arch, "files": entries}, pretty = True)
    return description

def _assemble_impl(
    actions: AnalysisActions,
    arch: str,
    author_hook: _AuthorHook | None,
    described: dict[str, _Described],
    description_name: str,
    directory: OutputArtifact,
    metadata: list[ArtifactValue],
    partitions: list[_Described],
    root_hashes: list[ArtifactValue],
) -> list[Provider]:
    files = dict(described)
    for index, value in enumerate(metadata):
        name = value.read_json()["published"]
        if name in files:
            fail("image_artifacts: {} is published by more than one target".format(name))
        files[name] = partitions[index]

    hashes = [value.read_string().strip() for value in root_hashes]
    published = {name: file.artifact for name, file in files.items()}
    if author_hook != None:
        # The author hook reads a description of the files it is given, which cannot list what it
        # writes itself and so differs from the release's. Each output is declared on its own, so that
        # Buck names a missing one as an output of the hook.
        given = _write_description(actions, "author_hook/description.json", arch, files, hashes)
        tree = actions.symlinked_dir("author_hook/files", published)
        authored = {name: actions.declare_output("author_hook/out", name) for name in author_hook.outputs}
        actions.run(
            cmd_args(
                author_hook.run,
                "--artifacts",
                given,
                "--files",
                tree,
                "--out-dir",
                cmd_args(authored.values()[0].as_output(), parent = 1),
                hidden = [artifact.as_output() for artifact in authored.values()],
            ),
            category = "author_hook",
        )
        for name, fields in author_hook.outputs.items():
            # Only a partition's name is unknown at analysis.
            if name in files:
                fail("image_artifacts: author_hook output {} is the name of a partition that one of the targets publishes".format(name))
            files[name] = _Described(
                artifact = authored[name],
                image = None,
                kind = fields["kind"],
                subkind = fields.get("subkind"),
                root_hash = None,
            )
            published[name] = authored[name]

    description = _write_description(actions, description_name, arch, files, hashes)
    release = published | {description_name: description}

    # Symlinks, not copies: a release is mostly disk images, and every one of them already exists.
    actions.symlinked_dir(directory, release)
    return []

_assemble = dynamic_actions(
    impl = _assemble_impl,
    attrs = {
        "arch": dynattrs.value(str),
        "author_hook": dynattrs.option(dynattrs.value(_AuthorHook)),
        "described": dynattrs.dict(str, dynattrs.value(_Described)),
        "description_name": dynattrs.value(str),
        "directory": dynattrs.output(),
        "metadata": dynattrs.list(dynattrs.artifact_value()),
        "partitions": dynattrs.list(dynattrs.value(_Described)),
        "root_hashes": dynattrs.list(dynattrs.artifact_value()),
    },
)

def _author_hook(
    run: RunInfo,
    outputs: dict[str, dict[str, str]],
    described: dict[str, _Described],
    description_name: str,
) -> _AuthorHook:
    for name, fields in outputs.items():
        check_name("author_hook output", name, FILENAME_PATTERN)
        if name in (".", ".."):
            fail("image_artifacts: author_hook output {!r} is not a file name".format(name))
        if name in described:
            fail("image_artifacts: author_hook output {} is already published by a target".format(name))
        if name == description_name:
            fail("image_artifacts: author_hook output {} is the name of the release's description".format(name))
        if not fields.get("kind") or fields.get("subkind") == "":
            fail("image_artifacts: author_hook output {} needs a kind and takes an optional subkind, got {}".format(name, fields))
    return _AuthorHook(outputs = outputs, run = run)

def _image_artifacts_impl(ctx: AnalysisContext) -> list[Provider]:
    if not ctx.attrs.targets:
        fail("image_artifacts: at least one target is required")

    arch = architecture.spelling(ctx.attrs._arch, "systemd")
    description_name = "artifacts_{}.json".format(arch)
    described = {}
    metadata = []
    partitions = []
    root_hashes = []
    for dep in ctx.attrs.targets:
        published = dep[PublishedInfo]

        # A DDI published whole carries its own verity root hash. A disk published as partitions
        # carries it on the partition verity protects.
        ddi = dep.get(DdiInfo)
        root_hash = None
        whole = None
        if ddi != None and ddi.root_hash != None:
            root_hash = len(root_hashes)
            root_hashes.append(ddi.root_hash.hash)
            if not published.partitions:
                whole = ddi.disk
        for name, file in published.artifacts.items():
            if name in described:
                fail("image_artifacts: {} is published by more than one target".format(name))
            if name == description_name:
                fail("image_artifacts: {} publishes {}, the name of the release's description".format(dep.label, name))
            described[name] = _Described(
                artifact = file.artifact,
                image = published.image,
                kind = file.kind,
                subkind = file.subkind,
                root_hash = root_hash if file.artifact == whole else None,
            )
        for partition in published.partitions:
            metadata.append(partition.metadata)
            partitions.append(
                _Described(
                    artifact = partition.blocks,
                    image = published.image,
                    kind = "partition",
                    subkind = partition.definition["type"],
                    root_hash = root_hash if partition.definition["verity"] == "data" else None,
                ),
            )

    # A hook without outputs would add nothing, so it does not run.
    author_hook = None
    if ctx.attrs.author_hook != None and ctx.attrs.author_hook_outputs:
        author_hook = _author_hook(ctx.attrs.author_hook[RunInfo], ctx.attrs.author_hook_outputs, described, description_name)

    directory = ctx.actions.declare_output("artifacts", dir = True)
    ctx.actions.dynamic_output_new(
        _assemble(
            arch = arch,
            author_hook = author_hook,
            described = described,
            description_name = description_name,
            directory = directory.as_output(),
            metadata = metadata,
            partitions = partitions,
            root_hashes = root_hashes,
        ),
    )
    return [DefaultInfo(default_output = directory)]

_image_artifacts = rule(
    impl = _image_artifacts_impl,
    attrs = {
        "author_hook": attrs.option(
            attrs.exec_dep(providers = [RunInfo]),
            default = None,
            doc = "a command that adds files to the release once its files are gathered (see docs/user/images.md)",
        ),
        "author_hook_outputs": attrs.dict(
            attrs.string(),
            attrs.dict(attrs.enum(["kind", "subkind"]), attrs.string()),
            default = {},
            doc = "each file the author hook writes, mapped to its kind and optional subkind in the release's description",
        ),
        "targets": attrs.list(
            attrs.dep(providers = [PublishedInfo]),
            doc = "the targets whose published artifacts make up the release",
        ),
        # The description is named after the architecture, so that the releases of several
        # architectures can share one directory.
        "_arch": attrs.string(default = architecture.configured()),
    },
    supports_incoming_transition = True,
)

def image_artifacts(name: str, distro: str | None = None, visibility: list[str] | None = None, **kwargs) -> None:
    """Gather the published artifacts of `targets`, once per distribution its package serves."""
    _image_artifacts(name = name, **(distribution.distributed(name, distro, visibility) | kwargs))
