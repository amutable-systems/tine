# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build a DDI, a GPT disk image, from partition definitions with systemd-repart."""

load("//:specs.bzl", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//platforms:architecture.bzl", "ARCHITECTURES", "architecture")
load(
    ":image.bzl",
    "GPT_LABEL_LIMIT",
    "IMAGE_TOOLS_ATTR",
    "ImageInfo",
    "ImageToolsInfo",
    "check_name",
    "declare_out",
    "pkgdb_paths",
    "spec_path",
    "terminal_image_command",
)
load(
    ":sign.bzl",
    "SigningKeyInfo",
    "VERITY_KEY_ATTR",
    "external_signing_execution",
    "merge_signing_access",
    "resolve_signing_key",
    "signing_key_spec",
)

Partition = dict[str, typing.Any]

# A byte count with an optional systemd size suffix, which every consumer reads base-1024.
SIZE_PATTERN = "^[0-9]+(\\.[0-9]+)?[KMGTPE]?$"

def partition(
    type: str,
    name: str | None = None,
    label: str | None = None,
    filesystem: str | None = None,
    copy_files: list[str] = [],
    size_min: str | int | None = None,
    size_max: str | int | None = None,
    minimize: str | None = None,
    compression: str | None = None,
    verity: str | None = None,
    verity_match_key: str | None = None,
) -> Partition:
    """Describe one partition without exposing a repart definition file."""
    if not type:
        fail("partition: type cannot be empty")
    if name == None:
        name = type
    check_name("partition artifact name", name)
    if label != None and len(label) > 36:
        fail("partition: label {!r} exceeds GPT's limit of 36 characters".format(label))
    if minimize not in (None, "off", "best", "guess"):
        fail("partition: invalid minimize value {!r}".format(minimize))
    if verity not in (None, "data", "hash", "signature"):
        fail("partition: invalid verity value {!r}".format(verity))
    if (verity == None) != (verity_match_key == None):
        fail("partition: verity and verity_match_key must be specified together")
    for copy in copy_files:
        if not copy:
            fail("partition: copy_files entries cannot be empty")

    # The driver derives partition UUIDs from these bytes, so the key order is part of the
    # contract: changing it re-identifies every partition of every unchanged image.
    # @unsorted-dict-items
    return {
        "name": name,
        "type": type,
        "label": label,
        "filesystem": filesystem,
        "copy_files": copy_files,
        "size_min": size_min,
        "size_max": size_max,
        "minimize": minimize,
        "compression": compression,
        "verity": verity,
        "verity_match_key": verity_match_key,
    }

_ESP_PARTITION = partition(
    type = "esp",
    filesystem = "vfat",
    copy_files = ["/boot:/", "/efi:/"],
    size_min = "512M",
    size_max = "512M",
)

DEFAULT_ROOT_PARTITIONS = [
    _ESP_PARTITION,
    partition(
        type = "root",
        filesystem = "ext4",
        copy_files = ["/"],
        minimize = "guess",
    ),
]

def verity_partitions(data: Partition, signed: bool) -> list[Partition]:
    """Return `data`, its verity hash partition and, with `signed`, its verity signature partition.

    The labels of the hash and signature partitions are the label of `data` with a suffix.
    """
    kind = data["type"]
    label = data["label"]
    definitions = [
        data,
        partition(
            type = kind + "-verity",
            label = label + "_verity" if label != None else None,
            minimize = "best",
            verity = "hash",
            verity_match_key = kind,
        ),
    ]
    if signed:
        definitions.append(
            partition(
                type = kind + "-verity-sig",
                label = label + "_verity_sig" if label != None else None,
                verity = "signature",
                verity_match_key = kind,
            ),
        )
    return definitions

# systemd-sysupdate A/B slots match partitions by exact label, so the default layouts carry the
# image identity: `bootable_disk_image()` renders the placeholders from its image_id and version
# attributes (raw `ddi()` users render them with `format_partition_labels()`).
def _usr_verity_partitions(signed: bool) -> list[Partition]:
    usr = partition(
        type = "usr",
        label = "{image_id}_{version}",
        filesystem = "erofs",
        copy_files = ["/usr:/"],
        minimize = "best",
        compression = "zstd",
        verity = "data",
        verity_match_key = "usr",
    )
    return verity_partitions(usr, signed) + [_ESP_PARTITION]

DEFAULT_USR_VERITY_PARTITIONS = _usr_verity_partitions(signed = False)
DEFAULT_SIGNED_USR_VERITY_PARTITIONS = _usr_verity_partitions(signed = True)

def format_partition_labels(definitions: list[Partition], image_id: str, version: str) -> list[Partition]:
    """Render {image_id} and {version} placeholders in partition labels."""
    formatted = []
    for definition in definitions:
        label = definition["label"]
        if label == None or "{" not in label:
            formatted.append(definition)
            continue
        formatted.append(
            definition
            | {
                "label": label.format(image_id = image_id, version = version),
            }
        )

    # Rendering can only lengthen a label, so re-check GPT's limit on the final value.
    for definition in formatted:
        if definition["label"] != None and len(definition["label"]) > GPT_LABEL_LIMIT:
            fail(
                "partition: label {!r} exceeds GPT's limit of {} characters".format(
                    definition["label"],
                    GPT_LABEL_LIMIT,
                )
            )
    return formatted

def _check_definitions(
    definitions: list[Partition],
    *,
    imports: bool,
    disk: bool,
    split: bool,
    signed: bool,
) -> None:
    if not definitions and (split or not imports):
        fail("ddi: definitions may be empty only when assembling partition inputs")
    if not disk and not split:
        fail("ddi: at least one of disk or split must be enabled")
    for definition in definitions:
        label = definition["label"]
        if label != None and "{" in label:
            fail("ddi: unrendered placeholder in label {!r}; render with format_partition_labels()".format(label))
    signatures = [it for it in definitions if it["verity"] == "signature"]
    if bool(signatures) != signed:
        fail("ddi: Verity=signature and signing credentials must be specified together")

WrittenPartitionInfo = record(
    definition = Partition,
    # What it carries; absent for a partition whose content no listing describes.
    manifest = Artifact | None,
)

PartitionInfo = record(
    definition = Partition,
    blocks = Artifact,
    metadata = Artifact,
    # What it carries; absent for the same reason. An imported partition brings this with it, so
    # the invocation that copies it in can list the whole disk without listing that partition.
    manifest = Artifact | None,
)

RootHashInfo = provider(
    doc = "A dm-verity root hash generated while creating independent partitions.",
    fields = {
        "hash": provider_field(Artifact),
        "kind": provider_field(str),
    },
)

DdiInfo = provider(
    doc = "A DDI: an optional composed disk and any independent partition artifacts.",
    fields = {
        "disk": provider_field(Artifact | None, default = None),
        # What the composed disk carries, its own partitions and its imported ones taken together.
        "manifest": provider_field(Artifact | None, default = None),
        "partitions": provider_field(list[PartitionInfo]),
        "root_hash": provider_field(RootHashInfo | None, default = None),
        # The partitions this invocation populates itself. Imported ones are in `partitions`
        # instead, already listed by the invocation that filled them.
        "written": provider_field(list[WrittenPartitionInfo], default = []),
    },
)

DdiOutput = record(
    info = DdiInfo,
    outputs = list[Artifact],
    sub_targets = dict[str, list[Provider]],
)

def _verity_kind(definitions: list[Partition]) -> str | None:
    kind = None
    for definition in definitions:
        if definition["verity"] != "data":
            continue
        if kind != None:
            fail("ddi: one invocation cannot produce more than one verity root hash")
        if definition["type"].startswith("usr"):
            kind = "usr"
        elif definition["type"].startswith("root"):
            kind = "root"
        else:
            fail("ddi: Verity=data requires a usr or root partition type")
    return kind

def _partition_sub_targets(partitions: list[PartitionInfo]) -> dict[str, list[Provider]]:
    return {
        partition.definition["name"]: [
            DefaultInfo(default_output = partition.blocks, other_outputs = [partition.metadata]),
        ]
        for partition in partitions
    }

def _output(artifact: Artifact | None) -> OutputArtifact | None:
    return artifact.as_output() if artifact != None else None

def manifest_sub_targets(written: list[WrittenPartitionInfo]) -> dict[str, list[Provider]]:
    """What each partition carries, under the name of the definition that carries it."""
    listings = {it.definition["name"]: it.manifest for it in written if it.manifest != None}
    if not listings:
        return {}
    return {
        "manifests": [
            DefaultInfo(
                default_outputs = listings.values(),
                sub_targets = {name: [DefaultInfo(default_output = out)] for name, out in listings.items()},
            ),
        ],
    }

def declare_ddi(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    image: ImageInfo,
    systemd_arch: str,
    definitions: list[Partition],
    disk: bool = True,
    split: bool = False,
    imported: list[DdiInfo] = [],
    imported_root_hash: RootHashInfo | None = None,
    seed: str | None = None,
    verity_key: SigningKeyInfo | None = None,
    strip_pkgdb: bool = False,
    mkfs_options: dict[str, list[str]] = {},
    basename: str = "image",
    identifier: str | None = None,
    extension: dict[str, typing.Any] | None = None,
    tmpfiles: list[str] | None = None,
) -> DdiOutput:
    """Declare the systemd-repart action that builds a DDI from an image and partition definitions.

    With strip_pkgdb, the partitions omit the package database wherever the image's package system
    keeps it, for a system nothing ever resolves packages in. The image's `[pkgdb]` and `[sbom]`
    subtargets still capture it from the tree itself. `extension` makes the driver package the tree
    as an extension (see extension.bzl). `tmpfiles` replaces the tmpfiles lines of `image`.
    """
    _check_definitions(
        definitions,
        disk = disk,
        imports = bool(imported),
        signed = verity_key != None,
        split = split,
    )

    # repart hands mkfs one variable per filesystem and splits it on whitespace, so an option
    # carrying any would silently arrive as two.
    for filesystem, options in mkfs_options.items():
        for option in options:
            if " " in option or "\t" in option:
                fail(
                    "ddi: {} mkfs option {!r} holds whitespace, which repart splits on".format(
                        filesystem,
                        option,
                    )
                )

    imported_partitions = []
    for info in imported:
        imported_partitions.extend(info.partitions)

    names = [definition["name"] for definition in definitions] + [partition.definition["name"] for partition in imported_partitions]
    if len(names) != len({name: True for name in names}):
        fail("ddi: new and imported partition names must be unique")

    definition_specs = []
    outputs = []
    new_partitions = []
    written = []
    for definition in definitions:
        name = definition["name"]

        # A definition that copies nothing carries nothing to list: verity partitions hold a hash
        # tree, and a filesystem created empty is populated by the system that grows it rather than
        # by this build.
        listing = None
        if definition["copy_files"]:
            listing = declare_out(ctx, identifier, "manifests/{}.Uapi16Manifest".format(name))

        # A partition leaves this invocation as an artifact of its own only when it splits them
        # back out; otherwise it exists solely inside the disk they are composed into.
        blocks = None
        metadata = None
        if split:
            blocks = declare_out(ctx, identifier, "partitions/{}.raw".format(name))
            metadata = declare_out(ctx, identifier, "partitions/{}.json".format(name))
            outputs.append(blocks)
            new_partitions.append(
                PartitionInfo(
                    definition = definition,
                    blocks = blocks,
                    manifest = listing,
                    metadata = metadata,
                ),
            )

        definition_specs.append({
            "blocks": _output(blocks),
            "definition": definition,
            "manifest": _output(listing),
            "metadata": _output(metadata),
        })
        written.append(WrittenPartitionInfo(definition = definition, manifest = listing))

    spec = {
        # repart has no partition type for any architecture, so an `_any` DDI gets the build host's types.
        "architecture": None if systemd_arch == "_any" else systemd_arch,
        "basename": basename,
        "definitions": definition_specs,
        "extension": extension,
        # The default seed derives from the identity, so changing "repart" here changes the partition
        # UUIDs of every target that declares no identifier.
        "identity": "{}[{}]".format(ctx.label, identifier or "repart"),
        "manifest": None,
        "mkfs_options": mkfs_options,
        "out": None,
        "partitions": [
            {
                "blocks": value.blocks,
                "definition": value.definition,
                "manifest": value.manifest,
                "metadata": value.metadata,
            }
            for value in imported_partitions
        ],
        "pkgdb_paths": pkgdb_paths(image) if strip_pkgdb else [],
        "root_hash_out": None,
        "seed": seed,
        "signing": signing_key_spec(verity_key),
    }

    if tmpfiles != None:
        spec["tmpfiles"] = tmpfiles

    out = None
    manifest = None
    if disk:
        out = declare_out(ctx, identifier, basename + ".raw")
        spec["out"] = out.as_output()

        # Nothing to assemble a listing of the whole from when no part of this disk was listed,
        # which is a disk of imported verity partitions and nothing else.
        listed = [it for it in imported_partitions + written if it.manifest != None]
        if listed:
            manifest = declare_out(ctx, identifier, basename + ".Uapi16Manifest")
            spec["manifest"] = manifest.as_output()

    kind = _verity_kind(definitions)
    if kind != None and imported_root_hash != None:
        fail("ddi: cannot combine multiple verity root hashes")
    root_hash = imported_root_hash
    if kind != None:
        # The root hash file is named after the disk, so that a release can publish the root
        # hashes of several DDIs.
        hash_output = declare_out(ctx, identifier, "{}.{}hash".format(basename, kind))
        spec["root_hash_out"] = hash_output.as_output()
        root_hash = RootHashInfo(hash = hash_output, kind = kind)

    available_partitions = imported_partitions + new_partitions
    info = DdiInfo(
        disk = out,
        manifest = manifest,
        partitions = available_partitions,
        root_hash = root_hash,
        written = written,
    )
    sub_targets = manifest_sub_targets(written)
    if manifest != None:
        # Qualified, unlike the provider field: a logical image publishes a `manifest` subtarget of
        # its own (image.bzl:image_metadata_subtargets) listing its whole tree, and a composition
        # merges both sets of subtargets into one namespace.
        sub_targets["disk-manifest"] = [DefaultInfo(default_output = manifest)]
    if root_hash != None:
        sub_targets["roothash"] = [DefaultInfo(default_output = root_hash.hash), info]

    if out != None:
        outputs = [out]
        if available_partitions:
            sub_targets["partitions"] = [
                DefaultInfo(
                    default_outputs = [partition.blocks for partition in available_partitions],
                    sub_targets = _partition_sub_targets(available_partitions),
                ),
                info,
            ]
    else:
        sub_targets.update(_partition_sub_targets(new_partitions))
    signing_access = merge_signing_access([verity_key])
    ctx.actions.run(
        terminal_image_command(
            ctx,
            signing_access = signing_access,
            driver = "ddi",
            exe = tools.ddi,
            identifier = identifier,
            image = image,
            spec = spec,
        ),
        category = "ddi_split" if split else "ddi",
        identifier = identifier or "ddi",
        **external_signing_execution(signing_access),
    )
    return DdiOutput(info = info, outputs = outputs, sub_targets = sub_targets)

def _ddi_impl(ctx: AnalysisContext) -> list[Provider]:
    imported = []
    imported_root_hash = None
    for dep in ctx.attrs.partitions:
        info = dep[DdiInfo]
        imported.append(info)
        root_hash = info.root_hash
        if root_hash != None:
            if imported_root_hash != None:
                fail("ddi: cannot combine multiple verity root hashes")
            imported_root_hash = root_hash
    result = declare_ddi(
        ctx,
        tools = ctx.attrs._tools[ImageToolsInfo],
        systemd_arch = ARCHITECTURES[ctx.attrs._arch].systemd,
        basename = ctx.attrs.basename,
        definitions = [json.decode(value) for value in ctx.attrs.definitions],
        disk = ctx.attrs.disk,
        image = ctx.attrs.image[ImageInfo],
        imported = imported,
        imported_root_hash = imported_root_hash,
        mkfs_options = ctx.attrs.mkfs_options,
        seed = ctx.attrs.seed,
        split = ctx.attrs.split,
        strip_pkgdb = ctx.attrs.strip_pkgdb,
        verity_key = resolve_signing_key(ctx.attrs.verity_key),
    )
    return [
        DefaultInfo(default_outputs = result.outputs, sub_targets = result.sub_targets),
        result.info,
    ]

DDI_ATTRS = {
    "basename": attrs.string(
        default = "image",
        doc = "what the composed disk and its split partitions are named after, without extension",
    ),
    "definitions": attrs.list(attrs.string(), doc = "serialized partition definitions"),
    "mkfs_options": attrs.dict(
        attrs.string(),
        attrs.list(attrs.string()),
        default = {},
        doc = "filesystem -> the options mkfs is given when formatting a partition of that type",
    ),
    "seed": attrs.option(
        attrs.string(),
        default = None,
        doc = "explicit GPT/partition UUID seed; by default derive one from target identity and definitions",
    ),
    "strip_pkgdb": attrs.bool(
        default = False,
        doc = "leave the package database out of the partitions; the image still carries it",
    ),
    "verity_key": VERITY_KEY_ATTR,
}

_ddi = rule(
    impl = _ddi_impl,
    attrs = DDI_ATTRS
    | {
        "disk": attrs.bool(default = True, doc = "emit a composed raw disk image"),
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image used to populate new partitions"),
        "partitions": attrs.list(
            attrs.dep(providers = [DdiInfo]),
            default = [],
            doc = "split ddi targets whose partitions should be copied into the result",
        ),
        "split": attrs.bool(default = False, doc = "also emit independent artifacts for new partitions"),
        # `_arch` is the architecture the image runs on. repart picks the partition types for it.
        "_arch": attrs.string(default = architecture.configured()),
    }
    | IMAGE_TOOLS_ATTR,
)

def ddi(name: str, definitions: list[Partition], **kwargs) -> None:
    """Create a disk, independent partitions, or both."""
    _ddi(
        name = name,
        definitions = [json.encode(definition) for definition in definitions],
        **kwargs,
    )

# The disk conversion output formats, doubling as each artifact's extension.
DISK_FORMATS = ["qcow2", "raw.zst", "vmdk"]

# The formats whose disk has a size of its own, so it can leave room past the partitions for first boot to
# claim. The raw itself stays as small as its partitions.
VIRTUAL_SIZE_FORMATS = ["qcow2", "vmdk"]

VIRTUAL_SIZE_ATTR = attrs.option(
    attrs.string(),
    default = None,
    doc = 'size of the disk a qcow2 or vmdk describes, e.g. "20G"; the room past the partitions stays free',
)

DiskConversionInfo = provider(
    doc = "A composed raw disk re-encoded into one distributable format.",
    fields = {
        "format": provider_field(str),
        "image": provider_field(Artifact),
    },
)

def declare_disk_conversion(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    disk: DdiInfo,
    box: Dependency,
    format: str,
    virtual_size: str | None = None,
    basename: str = "image",
    identifier: str | None = None,
) -> DiskConversionInfo:
    """Declare a disk format conversion from a resolved raw disk provider.

    `virtual_size` sizes the disk of a format in VIRTUAL_SIZE_FORMATS, which otherwise is as large as
    the raw.
    """
    if disk.disk == None:
        fail("disk_convert: DdiInfo does not contain a composed disk")
    if virtual_size != None:
        if format not in VIRTUAL_SIZE_FORMATS:
            fail("disk_convert: a {} disk is the raw's size and takes no virtual_size".format(format))
        if not regex_match(SIZE_PATTERN, virtual_size):
            fail("disk_convert: invalid virtual_size {!r}".format(virtual_size))
    out = declare_out(ctx, identifier, basename + "." + format)
    cmd = cmd_args(
        box_run(box = box[BoxInfo], exe = tools.convert),
        spec_args(
            ctx.actions,
            spec_path(identifier, "convert"),
            {
                "format": format,
                "input": disk.disk,
                "out": out.as_output(),
                "virtual_size": virtual_size,
            },
        ),
    )
    ctx.actions.run(cmd, category = "disk_convert", identifier = identifier or format)
    return DiskConversionInfo(format = format, image = out)

def _disk_convert_impl(ctx: AnalysisContext) -> list[Provider]:
    info = declare_disk_conversion(
        ctx,
        tools = ctx.attrs._tools[ImageToolsInfo],
        basename = ctx.attrs.basename,
        disk = ctx.attrs.disk[DdiInfo],
        box = ctx.attrs.box,
        format = ctx.attrs.format,
        virtual_size = ctx.attrs.virtual_size,
    )
    return [DefaultInfo(default_output = info.image), info]

disk_convert = rule(
    impl = _disk_convert_impl,
    attrs = {
        "basename": attrs.string(default = "image", doc = "file name of the re-encoded disk, without extension"),
        "box": attrs.exec_dep(providers = [BoxInfo], doc = "execution environment supplying conversion tools"),
        "disk": attrs.dep(providers = [DdiInfo], doc = "the composed raw disk to re-encode"),
        "format": attrs.enum(DISK_FORMATS),
        "virtual_size": VIRTUAL_SIZE_ATTR,
    }
    | IMAGE_TOOLS_ATTR,
)
