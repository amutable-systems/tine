"""Partition and raw-disk assembly with systemd-repart."""

load("//:specs.bzl", "spec_args")
load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load(
    "//image:image.bzl",
    "IMAGE_TOOLS_ATTR",
    "ImageInfo",
    "ImageToolsInfo",
    "check_name",
    "declare_out",
    "spec_path",
    "terminal_image_command",
)
load("//image:sign.bzl", "SigningKeyInfo", "resolve_signing_key")

Partition = dict[str, typing.Any]

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

# systemd-sysupdate A/B slots match partitions by exact label, so the default layouts carry the
# image identity: `bootable_disk_image()` renders the placeholders from its image_id and version
# attributes (raw `repart()` users render them with `format_partition_labels()`).
def _usr_verity_partitions(signed: bool) -> list[Partition]:
    label = "{image_id}_{version}"
    definitions = [
        partition(
            name = "usr",
            type = "usr",
            label = label,
            filesystem = "erofs",
            copy_files = ["/usr:/"],
            minimize = "best",
            compression = "zstd",
            verity = "data",
            verity_match_key = "usr",
        ),
        partition(
            name = "usr-verity",
            type = "usr-verity",
            label = label + "_verity",
            minimize = "best",
            verity = "hash",
            verity_match_key = "usr",
        ),
    ]
    if signed:
        definitions.append(
            partition(
                name = "usr-verity-sig",
                type = "usr-verity-sig",
                label = label + "_verity_sig",
                verity = "signature",
                verity_match_key = "usr",
            )
        )
    return definitions + [_ESP_PARTITION]

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
        if definition["label"] != None and len(definition["label"]) > 36:
            fail(
                "partition: label {!r} exceeds GPT's limit of 36 characters".format(
                    definition["label"],
                )
            )
    return formatted

def encode_definitions(
    definitions: list[Partition],
    *,
    imports: bool,
    disk: bool,
    split: bool,
    signed: bool,
    rendered: bool = True,
) -> list[str]:
    """Validate a partition layout and serialize it for an attribute.

    `rendered = False` accepts label placeholders for a caller that renders them itself.
    """
    if not definitions and (split or not imports):
        fail("repart: definitions may be empty only when assembling partition inputs")
    if not disk and not split:
        fail("repart: at least one of disk or split must be enabled")
    names = {}
    for definition in definitions:
        if definition["name"] in names:
            fail("repart: definition names must be unique")
        names[definition["name"]] = True
        label = definition["label"]
        if rendered and label != None and "{" in label:
            fail("repart: unrendered placeholder in label {!r}; render with format_partition_labels()".format(label))
    signatures = [it for it in definitions if it["verity"] == "signature"]
    if bool(signatures) != signed:
        fail("repart: Verity=signature and signing credentials must be specified together")
    return [json.encode(definition) for definition in definitions]

PartitionInfo = record(
    definition = Partition,
    blocks = Artifact,
    metadata = Artifact,
)

RootHashInfo = provider(
    doc = "A dm-verity root hash generated while creating independent partitions.",
    fields = {
        "hash": provider_field(Artifact),
        "kind": provider_field(str),
    },
)

RepartInfo = provider(
    doc = "A repart result containing an optional disk and any independent partition artifacts.",
    fields = {
        "disk": provider_field(Artifact | None, default = None),
        "partitions": provider_field(list[PartitionInfo]),
        "root_hash": provider_field(RootHashInfo | None, default = None),
    },
)

RepartOutput = record(
    info = RepartInfo,
    outputs = list[Artifact],
    sub_targets = dict[str, list[Provider]],
)

def _verity_kind(definitions: list[Partition]) -> str | None:
    kind = None
    for definition in definitions:
        if definition["verity"] != "data":
            continue
        if kind != None:
            fail("repart: one invocation cannot produce more than one verity root hash")
        if definition["type"].startswith("usr"):
            kind = "usr"
        elif definition["type"].startswith("root"):
            kind = "root"
        else:
            fail("repart: Verity=data requires a usr or root partition type")
    return kind

def _partition_sub_targets(partitions: list[PartitionInfo]) -> dict[str, list[Provider]]:
    return {
        partition.definition["name"]: [
            DefaultInfo(default_output = partition.blocks, other_outputs = [partition.metadata]),
        ]
        for partition in partitions
    }

def declare_repart(
    ctx: AnalysisContext,
    *,
    image: ImageInfo,
    definitions: list[str],
    disk: bool = True,
    split: bool = False,
    imported: list[RepartInfo] = [],
    imported_root_hash: RootHashInfo | None = None,
    seed: str | None = None,
    verity_key: SigningKeyInfo | None = None,
    basename: str = "image",
    identifier: str | None = None,
) -> RepartOutput:
    """Declare repart actions from resolved image and partition providers."""
    decoded = [json.decode(value) for value in definitions]
    encode_definitions(
        decoded,
        disk = disk,
        imports = bool(imported),
        signed = verity_key != None,
        split = split,
    )

    imported_partitions = []
    for info in imported:
        imported_partitions.extend(info.partitions)

    names = [definition["name"] for definition in decoded] + [partition.definition["name"] for partition in imported_partitions]
    if len(names) != len({name: True for name in names}):
        fail("repart: new and imported partition names must be unique")

    spec = {
        "certificate": verity_key.certificate if verity_key else None,
        "definitions": decoded,
        "identity": "{}[{}]".format(ctx.label, identifier or "repart"),
        "out": None,
        "partitions": [
            {
                "blocks": value.blocks,
                "definition": value.definition,
                "metadata": value.metadata,
            }
            for value in imported_partitions
        ],
        "private_key": verity_key.private_key if verity_key else None,
        "root_hash_out": None,
        "seed": seed,
        "split_outputs": [],
    }

    outputs = []
    new_partitions = []
    if split:
        for definition in decoded:
            name = definition["name"]
            blocks = declare_out(ctx, identifier, "partitions/{}.raw".format(name))
            metadata = declare_out(ctx, identifier, "partitions/{}.json".format(name))
            spec["split_outputs"].append({
                "blocks": blocks.as_output(),
                "metadata": metadata.as_output(),
                "name": name,
            })
            outputs.append(blocks)
            new_partitions.append(
                PartitionInfo(
                    definition = definition,
                    blocks = blocks,
                    metadata = metadata,
                )
            )

    out = None
    if disk:
        out = declare_out(ctx, identifier, basename + ".raw")
        spec["out"] = out.as_output()

    kind = _verity_kind(decoded)
    if kind != None and imported_root_hash != None:
        fail("repart: cannot combine multiple verity root hashes")
    root_hash = imported_root_hash
    if kind != None:
        hash_output = declare_out(ctx, identifier, "{}hash".format(kind))
        spec["root_hash_out"] = hash_output.as_output()
        root_hash = RootHashInfo(hash = hash_output, kind = kind)

    available_partitions = imported_partitions + new_partitions
    info = RepartInfo(
        disk = out,
        partitions = available_partitions,
        root_hash = root_hash,
    )
    sub_targets = {}
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
    ctx.actions.run(
        terminal_image_command(
            ctx,
            driver = "repart",
            exe = ctx.attrs._tools[ImageToolsInfo].disk,
            identifier = identifier,
            image = image,
            spec = spec,
        ),
        category = "repart_split" if split else "repart",
        identifier = identifier or "repart",
    )
    return RepartOutput(info = info, outputs = outputs, sub_targets = sub_targets)

def _repart_impl(ctx: AnalysisContext) -> list[Provider]:
    imported = []
    imported_root_hash = None
    for dep in ctx.attrs.partitions:
        info = dep[RepartInfo]
        imported.append(info)
        root_hash = info.root_hash
        if root_hash != None:
            if imported_root_hash != None:
                fail("repart: cannot combine multiple verity root hashes")
            imported_root_hash = root_hash
    result = declare_repart(
        ctx,
        basename = ctx.attrs.basename,
        definitions = ctx.attrs.definitions,
        disk = ctx.attrs.disk,
        image = ctx.attrs.image[ImageInfo],
        imported = imported,
        imported_root_hash = imported_root_hash,
        seed = ctx.attrs.seed,
        split = ctx.attrs.split,
        verity_key = resolve_signing_key(ctx.attrs.verity_key),
    )
    return [
        DefaultInfo(default_outputs = result.outputs, sub_targets = result.sub_targets),
        result.info,
    ]

REPART_ATTRS = {
    "basename": attrs.string(default = "image", doc = "file name of the composed disk, without extension"),
    "definitions": attrs.list(attrs.string(), doc = "serialized partition definitions"),
    "seed": attrs.option(
        attrs.string(),
        default = None,
        doc = "explicit GPT/partition UUID seed; by default derive one from target identity and definitions",
    ),
    "verity_key": attrs.option(
        attrs.dep(providers = [SigningKeyInfo]),
        default = None,
        doc = "key signing the verity signature partition",
    ),
}

_repart = rule(
    impl = _repart_impl,
    attrs = REPART_ATTRS
    | {
        "disk": attrs.bool(default = True, doc = "emit a composed raw disk image"),
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image used to populate new partitions"),
        "partitions": attrs.list(
            attrs.dep(providers = [RepartInfo]),
            default = [],
            doc = "split repart targets whose partitions should be copied into the result",
        ),
        "split": attrs.bool(default = False, doc = "also emit independent artifacts for new partitions"),
    }
    | IMAGE_TOOLS_ATTR,
)

def repart(
    name: str,
    definitions: list[Partition],
    partitions: list[str] = [],
    disk: bool = True,
    split: bool = False,
    verity_key: str | None = None,
    **kwargs,
) -> None:
    """Create a disk, independent partitions, or both."""
    _repart(
        name = name,
        definitions = encode_definitions(
            definitions,
            disk = disk,
            imports = bool(partitions),
            signed = verity_key != None,
            split = split,
        ),
        partitions = partitions,
        disk = disk,
        split = split,
        verity_key = verity_key,
        **kwargs,
    )

# The disk conversion output formats, doubling as each artifact's extension.
DISK_FORMATS = ["qcow2", "raw.zst"]

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
    disk: RepartInfo,
    engine: Dependency,
    format: str,
    basename: str = "image",
    identifier: str | None = None,
) -> DiskConversionInfo:
    """Declare a disk format conversion from a resolved raw disk provider."""
    if disk.disk == None:
        fail("disk_convert: RepartInfo does not contain a composed disk")
    out = declare_out(ctx, identifier, basename + "." + format)
    cmd = cmd_args(
        chroot_run(engine = engine[EngineInfo], exe = ctx.attrs._tools[ImageToolsInfo].convert),
        spec_args(
            ctx,
            spec_path(identifier, "convert"),
            {
                "format": format,
                "input": disk.disk,
                "out": out.as_output(),
            },
        ),
    )
    ctx.actions.run(cmd, category = "disk_convert", identifier = identifier or format)
    return DiskConversionInfo(format = format, image = out)

def _disk_convert_impl(ctx: AnalysisContext) -> list[Provider]:
    info = declare_disk_conversion(
        ctx,
        basename = ctx.attrs.basename,
        disk = ctx.attrs.disk[RepartInfo],
        engine = ctx.attrs.engine,
        format = ctx.attrs.format,
    )
    return [DefaultInfo(default_output = info.image), info]

disk_convert = rule(
    impl = _disk_convert_impl,
    attrs = {
        "basename": attrs.string(default = "image", doc = "file name of the re-encoded disk, without extension"),
        "disk": attrs.dep(providers = [RepartInfo], doc = "the composed raw disk to re-encode"),
        "engine": attrs.dep(providers = [EngineInfo], doc = "execution environment supplying conversion tools"),
        "format": attrs.enum(DISK_FORMATS),
    }
    | IMAGE_TOOLS_ATTR,
)
