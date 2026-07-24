"""Partition and raw-disk assembly with systemd-repart."""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load("//image:layer.bzl", "ImageInfo")

# buildifier: disable=name-conventions  (record types, conventionally UpperCamelCase)
Partition = record(
    name = str,
    type = str,
    label = field(str | None, default = None),
    filesystem = field(str | None, default = None),
    copy_files = field(list[str], default = []),
    size_min = field(str | int | None, default = None),
    size_max = field(str | int | None, default = None),
    minimize = field(str | None, default = None),
    compression = field(str | None, default = None),
    verity = field(str | None, default = None),
    verity_match_key = field(str | None, default = None),
)

# buildifier: disable=function-docstring-args
# buildifier: disable=function-docstring-return
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
        verity_match_key: str | None = None) -> Partition:
    """Describe one partition without exposing a repart definition file."""
    if not type:
        fail("partition: type cannot be empty")
    if name == None:
        name = type
    if not regex_match("^[a-zA-Z0-9._-]+$", name):
        fail("partition: invalid artifact name {!r}".format(name))
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
    return Partition(
        name = name,
        type = type,
        label = label,
        filesystem = filesystem,
        copy_files = copy_files,
        size_min = size_min,
        size_max = size_max,
        minimize = minimize,
        compression = compression,
        verity = verity,
        verity_match_key = verity_match_key,
    )

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
        definitions.append(partition(
            name = "usr-verity-sig",
            type = "usr-verity-sig",
            label = label + "_verity_sig",
            verity = "signature",
            verity_match_key = "usr",
        ))
    return definitions + [_ESP_PARTITION]

DEFAULT_USR_VERITY_PARTITIONS = _usr_verity_partitions(signed = False)
DEFAULT_SIGNED_USR_VERITY_PARTITIONS = _usr_verity_partitions(signed = True)

# buildifier: disable=function-docstring-args
# buildifier: disable=function-docstring-return
def format_partition_labels(
        definitions: list[Partition],
        image_id: str,
        version: str) -> list[Partition]:
    """Render {image_id} and {version} placeholders in partition labels."""
    formatted = []
    for definition in definitions:
        if definition.label == None or "{" not in definition.label:
            formatted.append(definition)
            continue
        formatted.append(partition(
            type = definition.type,
            name = definition.name,
            label = definition.label.format(image_id = image_id, version = version),
            filesystem = definition.filesystem,
            copy_files = definition.copy_files,
            size_min = definition.size_min,
            size_max = definition.size_max,
            minimize = definition.minimize,
            compression = definition.compression,
            verity = definition.verity,
            verity_match_key = definition.verity_match_key,
        ))
    return formatted

# buildifier: disable=name-conventions  (record type, conventionally UpperCamelCase)
PartitionInfo = record(
    definition = Partition,
    blocks = Artifact,
    metadata = Artifact,
)

RepartInfo = provider(
    doc = "Independent partition artifacts produced by a split repart invocation.",
    fields = {
        "engine": provider_field(Dependency),
        "partitions": provider_field(list[PartitionInfo]),
    },
)

RootHashInfo = provider(
    doc = "A dm-verity root hash generated while creating independent partitions.",
    fields = {
        "hash": provider_field(Artifact),
        "kind": provider_field(str),
    },
)

DiskImageInfo = provider(
    doc = "A raw disk and any independently available constituent partitions.",
    fields = {
        "engine": provider_field(Dependency),
        "image": provider_field(Artifact),
        "partitions": provider_field(list[PartitionInfo]),
        "source": provider_field(Dependency),
    },
)

ConvertedDiskInfo = provider(
    doc = "A composed raw disk image re-encoded into a distributable output format.",
    fields = {
        "format": provider_field(str),
        "image": provider_field(Artifact),
        "source": provider_field(Dependency),
    },
)

def _partition_dict(value: Partition) -> dict[str, typing.Any]:
    return {
        "name": value.name,
        "type": value.type,
        "label": value.label,
        "filesystem": value.filesystem,
        "copy_files": value.copy_files,
        "size_min": value.size_min,
        "size_max": value.size_max,
        "minimize": value.minimize,
        "compression": value.compression,
        "verity": value.verity,
        "verity_match_key": value.verity_match_key,
    }

def _partition_from_dict(value: dict[str, typing.Any]) -> Partition:
    return Partition(
        name = value["name"],
        type = value["type"],
        label = value["label"],
        filesystem = value["filesystem"],
        copy_files = value["copy_files"],
        size_min = value["size_min"],
        size_max = value["size_max"],
        minimize = value["minimize"],
        compression = value["compression"],
        verity = value["verity"],
        verity_match_key = value["verity_match_key"],
    )

def _verity_kind(definitions: list[Partition]) -> str | None:
    kind = None
    for definition in definitions:
        if definition.verity != "data":
            continue
        if kind != None:
            fail("repart: one invocation cannot produce more than one verity root hash")
        if definition.type.startswith("usr"):
            kind = "usr"
        elif definition.type.startswith("root"):
            kind = "root"
        else:
            fail("repart: Verity=data requires a usr or root partition type")
    return kind

def _partition_sub_targets(partitions: list[PartitionInfo]) -> dict[str, list[Provider]]:
    return {
        partition.definition.name: [
            DefaultInfo(default_output = partition.blocks, other_outputs = [partition.metadata]),
        ]
        for partition in partitions
    }

def _repart_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    definitions = [_partition_from_dict(json.decode(value)) for value in ctx.attrs.definitions]
    imported = []
    imported_root_hash = None
    for dep in ctx.attrs.partitions:
        info = dep[RepartInfo]
        if info.engine != image.engine:
            fail("repart: image and imported partitions must use the same engine")
        imported.extend(info.partitions)
        root_hash = dep.get(RootHashInfo)
        if root_hash != None:
            if imported_root_hash != None:
                fail("repart: cannot combine multiple verity root hashes")
            imported_root_hash = root_hash
    names = [definition.name for definition in definitions] + [
        partition.definition.name
        for partition in imported
    ]
    if len(names) != len({name: True for name in names}):
        fail("repart: new and imported partition names must be unique")

    cmd = cmd_args(
        chroot_run(engine = image.engine[EngineInfo], exe = ctx.attrs._driver),
        "--identity",
        str(ctx.label),
    )
    if ctx.attrs.seed != None:
        cmd.add("--seed", ctx.attrs.seed)
    if ctx.attrs.private_key != None:
        cmd.add("--private-key", ctx.attrs.private_key)
    if ctx.attrs.certificate != None:
        cmd.add("--certificate", ctx.attrs.certificate)
    for lower in image.layers:
        cmd.add("--lower", lower)
    for snippet in image.tmpfiles:
        cmd.add("--tmpfiles", snippet)
    for definition in definitions:
        cmd.add("--definition", json.encode(_partition_dict(definition)))
    for value in imported:
        cmd.add(
            "--partition",
            json.encode(_partition_dict(value.definition)),
            value.blocks,
            value.metadata,
        )

    outputs = []
    partitions = []
    if ctx.attrs.split:
        for definition in definitions:
            blocks = ctx.actions.declare_output("partitions/{}.raw".format(definition.name))
            metadata = ctx.actions.declare_output("partitions/{}.json".format(definition.name))
            cmd.add("--split-output", definition.name, blocks.as_output(), metadata.as_output())
            outputs.append(blocks)
            partitions.append(PartitionInfo(definition = definition, blocks = blocks, metadata = metadata))

    out = None
    if ctx.attrs.disk:
        out = ctx.actions.declare_output("image.raw")
        cmd.add("--out", out.as_output())

    extra_providers = []
    kind = _verity_kind(definitions)
    if kind != None and imported_root_hash != None:
        fail("repart: cannot combine multiple verity root hashes")
    root_hash = imported_root_hash
    if kind != None:
        hash_output = ctx.actions.declare_output("{}hash".format(kind))
        cmd.add("--root-hash-out", hash_output.as_output())
        root_hash = RootHashInfo(hash = hash_output, kind = kind)
    if root_hash != None:
        extra_providers.append(root_hash)

    available_partitions = imported + partitions
    sub_targets = {}
    if root_hash != None:
        sub_targets["roothash"] = [DefaultInfo(default_output = root_hash.hash)]

    providers = []
    if out != None:
        if available_partitions:
            sub_targets["partitions"] = [
                DefaultInfo(
                    default_outputs = [partition.blocks for partition in available_partitions],
                    sub_targets = _partition_sub_targets(available_partitions),
                ),
            ]
        providers.extend([
            DefaultInfo(default_output = out, sub_targets = sub_targets),
            DiskImageInfo(
                engine = image.engine,
                image = out,
                partitions = available_partitions,
                source = ctx.attrs.image,
            ),
        ])
    else:
        sub_targets.update(_partition_sub_targets(partitions))
        providers.append(DefaultInfo(default_outputs = outputs, sub_targets = sub_targets))
    if ctx.attrs.split:
        providers.append(RepartInfo(engine = image.engine, partitions = available_partitions))

    category = "repart"
    if ctx.attrs.split:
        category += "_split"
    ctx.actions.run(cmd, category = category)
    return providers + extra_providers

_repart = rule(
    impl = _repart_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image used to populate new partitions"),
        "definitions": attrs.list(attrs.string(), doc = "serialized partition definitions"),
        "disk": attrs.bool(default = True, doc = "emit a composed raw disk image"),
        "partitions": attrs.list(
            attrs.dep(providers = [RepartInfo]),
            default = [],
            doc = "split repart targets whose partitions should be copied into the result",
        ),
        "split": attrs.bool(default = False, doc = "also emit independent artifacts for new partitions"),
        "seed": attrs.option(
            attrs.string(),
            default = None,
            doc = "explicit GPT/partition UUID seed; by default derive one from target identity and definitions",
        ),
        "private_key": attrs.option(attrs.source(), default = None, doc = "PEM key for verity signing"),
        "certificate": attrs.option(attrs.source(), default = None, doc = "PEM certificate for verity signing"),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:disk"),
    },
)

# buildifier: disable=function-docstring-args
def repart(
        name: str,
        definitions: list[Partition],
        partitions: list[str] = [],
        disk: bool = True,
        split: bool = False,
        private_key: str | None = None,
        certificate: str | None = None,
        **kwargs) -> None:
    """Create a disk, independent partitions, or both."""
    if not definitions and (split or not partitions):
        fail("repart: definitions may be empty only when assembling partition inputs")
    if not disk and not split:
        fail("repart: at least one of disk or split must be enabled")
    names = [definition.name for definition in definitions]
    if len(names) != len({name: True for name in names}):
        fail("repart: definition names must be unique")
    for definition in definitions:
        if definition.label != None and "{" in definition.label:
            fail("repart: unrendered placeholder in label {!r}; render with format_partition_labels()".format(
                definition.label,
            ))
    if (private_key == None) != (certificate == None):
        fail("repart: private_key and certificate must be specified together")
    signed = [definition for definition in definitions if definition.verity == "signature"]
    if bool(signed) != (private_key != None):
        fail("repart: Verity=signature and signing credentials must be specified together")
    _repart(
        name = name,
        definitions = [json.encode(_partition_dict(value)) for value in definitions],
        partitions = partitions,
        disk = disk,
        split = split,
        private_key = private_key,
        certificate = certificate,
        **kwargs
    )

# The disk conversion output formats, doubling as each artifact's extension.
DISK_FORMATS = ["qcow2", "raw.zst"]

def _disk_convert_impl(ctx: AnalysisContext) -> list[Provider]:
    disk = ctx.attrs.disk[DiskImageInfo]
    out = ctx.actions.declare_output("image." + ctx.attrs.format)
    cmd = cmd_args(
        chroot_run(engine = disk.engine[EngineInfo], exe = ctx.attrs._driver),
        "--format",
        ctx.attrs.format,
        "--input",
        disk.image,
        "--out",
        out.as_output(),
    )
    ctx.actions.run(cmd, category = "disk_convert", identifier = ctx.attrs.format)
    return [
        DefaultInfo(default_output = out),
        ConvertedDiskInfo(format = ctx.attrs.format, image = out, source = disk.source),
    ]

disk_convert = rule(
    impl = _disk_convert_impl,
    attrs = {
        "disk": attrs.dep(providers = [DiskImageInfo], doc = "the composed raw disk to re-encode"),
        "format": attrs.enum(DISK_FORMATS),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:convert"),
    },
)
