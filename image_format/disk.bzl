"""Raw disk image outputs."""

load("//engine:rules.bzl", "EngineInfo", "chroot_run")
load("//image:layer.bzl", "ImageInfo")

# buildifier: disable=name-conventions  (a record type, conventionally UpperCamelCase)
Partition = record(
    type = str,
    label = field(str | None, default = None),
    filesystem = field(str | None, default = None),
    copy_files = field(list[str], default = []),
    size_min = field(str | int | None, default = None),
    size_max = field(str | int | None, default = None),
    minimize = field(str | None, default = None),
)

def partition(
        type: str,
        label: str | None = None,
        filesystem: str | None = None,
        copy_files: list[str] = [],
        size_min: str | int | None = None,
        size_max: str | int | None = None,
        minimize: str | None = None) -> Partition:
    """Describe one partition without exposing a repart definition file."""
    if not type:
        fail("partition: type cannot be empty")
    if minimize not in (None, "off", "best", "guess"):
        fail("partition: invalid minimize value {!r}".format(minimize))
    return Partition(
        type = type,
        label = label,
        filesystem = filesystem,
        copy_files = copy_files,
        size_min = size_min,
        size_max = size_max,
        minimize = minimize,
    )

_DEFAULT_PARTITIONS = [
    partition(
        type = "esp",
        filesystem = "vfat",
        copy_files = ["/boot:/", "/efi:/"],
        size_min = "512M",
        size_max = "512M",
    ),
    partition(
        type = "root",
        filesystem = "ext4",
        copy_files = ["/"],
        minimize = "guess",
    ),
]

DiskImageInfo = provider(
    doc = "A raw disk image suitable for a VM runner or later disk-format conversion.",
    fields = {
        "engine": provider_field(Dependency),
        "image": provider_field(Artifact),
    },
)

def _render_partition(value: Partition) -> str:
    lines = ["[Partition]", "Type=" + value.type]
    for name, setting in (
        ("Label", value.label),
        ("Format", value.filesystem),
        ("SizeMinBytes", value.size_min),
        ("SizeMaxBytes", value.size_max),
        ("Minimize", value.minimize),
    ):
        if setting != None:
            lines.append("{}={}".format(name, setting))
    for copy in value.copy_files:
        if not copy:
            fail("partition: copy_files entries cannot be empty")
        lines.append("CopyFiles=" + copy)
    return "\n".join(lines) + "\n"

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
    for index, contents in enumerate(ctx.attrs.partition_definitions):
        definition = ctx.actions.write("partitions/partition-{}.conf".format(index), contents)
        cmd.add("--definition", definition)
    ctx.actions.run(cmd, category = "image_disk")
    return [
        DefaultInfo(default_output = out),
        DiskImageInfo(engine = image.engine, image = out),
    ]

_image_disk = rule(
    impl = _image_disk_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to write into the disk"),
        "seed": attrs.option(
            attrs.string(),
            default = None,
            doc = "explicit GPT/partition UUID seed; by default derive one from target identity and definitions",
        ),
        "partition_definitions": attrs.list(
            attrs.string(),
            doc = "rendered repart definitions in partition order",
        ),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:disk"),
    },
)

def image_disk(name: str, partitions: list[Partition] | None = None, **kwargs) -> None:
    """Materialize a logical image using an ordered partition layout."""
    if partitions == None:
        partitions = _DEFAULT_PARTITIONS
    if not partitions:
        fail("image_disk: partitions cannot be empty")
    _image_disk(
        name = name,
        partition_definitions = [_render_partition(value) for value in partitions],
        **kwargs
    )
