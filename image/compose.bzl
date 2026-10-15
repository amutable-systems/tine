# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Convenience compositions for image products."""

load("//box:runtime.bzl", "BoxInfo")
load("//distribution:defs.bzl", "distribution")
load("//package:manager.bzl", "PackageManagerInfo")
load("//platforms:architecture.bzl", "ARCHITECTURES")
load(":ami.bzl", "declare_ami")
load(
    ":archive.bzl",
    "ARCHIVE_ATTRS",
    "COMPRESSIONS",
    "ImageArchiveInfo",
    "declare_image_archive",
    "declare_image_directory",
)
load(
    ":boot.bzl",
    "UKI_ATTRS",
    "UkiProfile",  # @unused Used as a type.
    "boot_subtargets",
    "declare_boot_artifacts",
    "declare_uki",
    "encode_profiles",
    "uki_subtargets",
)
load(
    ":ddi.bzl",
    "DDI_ATTRS",
    "DISK_FORMATS",
    "Partition",  # @unused Used as a type.
    "VIRTUAL_SIZE_ATTR",
    "VIRTUAL_SIZE_FORMATS",
    "declare_ddi",
    "declare_disk_conversion",
    "format_partition_labels",
    "manifest_sub_targets",
)
load(
    ":extension.bzl",
    "EXTENSION_ATTRS",
    "ExtensionInfo",
    "PORTABLE_ATTRS",
    "declare_extension",
    "declare_portable",
)
load(
    ":image.bzl",
    "IMAGE_ATTRS",
    "ImageInfo",
    "ImageToolsInfo",  # @unused Used as a function argument type.
    "LayerOperation",  # @unused Used as a type.
    "LayerOperationTree",  # @unused Used as a type.
    "VERSION_PATTERN",
    "check_name",
    "copy",
    "declare_image",
    "flatten_operations",
    "generated",
    "image_providers",
    "install_systemd_boot",
    "merge_os_release",
    "remove",
    "sign_systemd_boot",
    "symlink",
    "write_file",
)
load(":publish.bzl", "PublishedInfo", "published_metadata_subtargets")
load(
    ":sign.bzl",
    "SigningKeyInfo",  # @unused Used as a function argument type.
    "resolve_signing_key",
)
load(":version.bzl", "resolve_version")

InitrdInfo = provider(
    doc = "A logical initrd image and its derived cpio archive.",
    fields = {
        "cpio": provider_field(ImageArchiveInfo),
        "image": provider_field(ImageInfo),
    },
)

# As of systemd 262, systemd-imds-early-network.service only writes a .network file, but doesn't start
# networkd. The IMDS generator pulls the unit in only on a cloud it detects in the hardware database, so
# networkd runs nowhere else, and `systemd.imds.network=off` opts out. mkosi does that via a preset call.
_IMDS_NETWORKD = write_file(
    "/usr/lib/systemd/system/systemd-imds-early-network.service.d/tine-networkd.conf",
    "[Unit]\nWants=systemd-networkd.service\n",
)

# EBS volumes are NVMe devices whose I/O can stall for longer than the kernel's default 30 s timeout,
# which then fails the root file system. AWS recommends the maximum, see
# https://docs.aws.amazon.com/ebs/latest/userguide/nvme-ebs-volumes.html#timeout-nvme-ebs-volumes.
# This is the rule of amazon-ec2-utils, 70-ec2-nvme-devices.rules: unlike `nvme_core.io_timeout` on the
# kernel command line, it leaves the timeout of every other NVMe disk alone. The root volume keeps the
# timeout set here after switching root.
_EBS_IO_TIMEOUT = write_file(
    "/usr/lib/udev/rules.d/70-ebs-io-timeout.rules",
    'KERNEL=="nvme[0-9]*n[0-9]*", ENV{DEVTYPE}=="disk", ATTRS{model}=="Amazon Elastic Block Store", ATTR{queue/io_timeout}="4294967295"\n',
)

# The portable profiles bind /etc/machine-id and /etc/resolv.conf from the host. systemd cannot create
# mount points on the read-only root partition, so the image must contain empty /etc/machine-id and
# /etc/resolv.conf files. The caller's operations run after _PORTABLE_MOUNT_POINTS, so an operation
# that writes /etc/machine-id or /etc/resolv.conf replaces the empty file.
_PORTABLE_MOUNT_POINTS = [
    write_file("/etc/machine-id"),
    write_file("/etc/resolv.conf"),
]

def _initrd_image_impl(ctx: AnalysisContext) -> list[Provider]:
    tools = ctx.attrs._tools[ImageToolsInfo]
    image = declare_image(
        ctx,
        tools = tools,
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = generated(
            [
                symlink("/usr/lib/systemd/systemd", "/init"),
                symlink("/etc/os-release", "/etc/initrd-release"),
                _IMDS_NETWORKD,
                _EBS_IO_TIMEOUT,
            ]
            + ctx.attrs.ops,
            ["initrd"],
        )
        # The removals run after the generators, because the hwdb generator reads hwdb.d. At run time
        # udev reads only the compiled hardware database. Nothing in the initrd shows catalog
        # messages or resolves service names, so the initrd does not need the catalogs or
        # /etc/services.
        + [
            remove("/usr/lib/udev/hwdb.d"),
            remove("/usr/lib/systemd/catalog"),
            remove("/var/lib/systemd/catalog"),
            remove("/etc/services"),
        ],
        package_manager = ctx.attrs.package_manager,
        # Each release lists the packages of its "initrd" package set, so the initrd names the set
        # instead of packages.
        package_sets = ["initrd"] + ctx.attrs.package_sets,
        packages = ctx.attrs.packages,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
    )
    cpio = declare_image_archive(
        ctx,
        tools = tools,
        compression = ctx.attrs.compression,
        format = "cpio",
        image = image,
        strip_pkgdb = ctx.attrs.strip_pkgdb,
    )
    return image_providers(
        default_outputs = [cpio.archive],
        extra = [InitrdInfo(cpio = cpio, image = image), cpio],
        image = image,
    )

_initrd_image = rule(
    impl = _initrd_image_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS
    | {
        "compression": attrs.enum(COMPRESSIONS, default = "zstd"),
        # An initrd is small, short-lived, and read once, so the defaults an OS image wants are the
        # wrong way round here: documentation and translations only cost boot memory.
        "install_docs": attrs.bool(default = False),
        "install_langs": attrs.list(attrs.string(), default = ["C.UTF-8"]),
        "package_manager": attrs.dep(providers = [PackageManagerInfo]),
        "strip_pkgdb": attrs.bool(
            default = True,
            doc = "leave the package database out of the cpio; the image still carries it",
        ),
    },
)

def initrd_image(
    name: str,
    ops: list[LayerOperationTree] = [],
    version: str | Select | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    **kwargs,
) -> None:
    """Declare the conventional initrd, extended by `packages`, `package_sets`, and `ops`.

    The release's `initrd` package set and the operations every initrd needs come first; what this
    target declares is added to them rather than replacing them. `bootable_disk_image` declares one
    of these for itself when it is given no `initrd`.
    """
    _initrd_image(
        name = name,
        ops = flatten_operations(ops),
        version = resolve_version("initrd_image {}".format(name), version),
        **(distribution.distributed(name, distro, visibility) | kwargs),
    )

_ROOT_ATTRS = {
    "package_manager": attrs.option(
        attrs.dep(providers = [PackageManagerInfo]),
        default = None,
    ),
    "parent": attrs.option(
        attrs.dep(providers = [ImageInfo]),
        default = None,
        doc = "the logical image to build on instead of starting one of its own",
    ),
}

def _composed_image(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    install_docs: bool,
    install_langs: list[str],
    ops: list[LayerOperation],
    package_sets: list[str],
    packages: list[str],
    tmpfiles: list[str],
    version: str,
    generate: bool = True,
    package_manager: Dependency | None = None,
    parent: ImageInfo | None = None,
) -> ImageInfo:
    if (parent == None) == (package_manager == None):
        fail("{}: exactly one of parent and package_manager is required".format(ctx.label.name))
    return declare_image(
        ctx,
        tools = tools,
        identifier = "image",
        install_docs = install_docs,
        install_langs = install_langs,
        ops = generated(ops, packages + package_sets) if generate else ops,
        package_manager = package_manager,
        package_sets = package_sets,
        packages = packages,
        parent = parent,
        tmpfiles = tmpfiles,
        version = version,
    )

def _archive_impl(ctx: AnalysisContext) -> list[Provider]:
    tools = ctx.attrs._tools[ImageToolsInfo]
    image = _composed_image(
        ctx,
        tools = tools,
        package_manager = ctx.attrs.package_manager,
        parent = ctx.attrs.parent[ImageInfo] if ctx.attrs.parent != None else None,
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = ctx.attrs.ops,
        package_sets = ctx.attrs.package_sets,
        packages = ctx.attrs.packages,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
    )
    archive = declare_image_archive(
        ctx,
        tools = tools,
        compression = ctx.attrs.compression,
        format = ctx.attrs.format,
        image = image,
    )
    return image_providers(
        default_outputs = [archive.archive],
        extra = [archive],
        image = image,
    )

_archive = rule(
    impl = _archive_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS | ARCHIVE_ATTRS | _ROOT_ATTRS,
)

def _extension_impl(ctx: AnalysisContext) -> list[Provider]:
    tools = ctx.attrs._tools[ImageToolsInfo]

    # An extension runs no generators. A database generated from the full stack would replace the
    # database of the parent in the merged tree. It would also be out of date when the parent changes.
    parent = None
    if ctx.attrs.parent != None:
        # An extension of an extension contains only its own delta. It works only when the
        # extension below it is merged too, and neither DDI records that dependency.
        if ctx.attrs.parent.get(ExtensionInfo) != None:
            fail("{}: parent {} is an extension itself; extend the image it extends".format(ctx.label.name, ctx.attrs.parent.label))
        parent = ctx.attrs.parent[ImageInfo]
    image = _composed_image(
        ctx,
        tools = tools,
        generate = False,
        package_manager = ctx.attrs.package_manager,
        parent = parent,
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = ctx.attrs.ops,
        package_sets = ctx.attrs.package_sets,
        packages = ctx.attrs.packages,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
    )
    return declare_extension(
        ctx,
        tools = tools,
        arch = ctx.attrs.arch,
        parent = parent,
        classes = ctx.attrs.classes,
        image = image,
        name = ctx.label.name,
        release = ctx.attrs.release,
        scope = ctx.attrs.scope,
        seed = ctx.attrs.seed,
        verity_key = resolve_signing_key(ctx.attrs.verity_key),
        version = ctx.attrs.version,
    )

_extension = rule(
    impl = _extension_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS | EXTENSION_ATTRS | _ROOT_ATTRS,
)

def _portable_impl(ctx: AnalysisContext) -> list[Provider]:
    image = _composed_image(
        ctx,
        tools = ctx.attrs._tools[ImageToolsInfo],
        package_manager = ctx.attrs.package_manager,
        parent = ctx.attrs.parent[ImageInfo] if ctx.attrs.parent != None else None,
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = _PORTABLE_MOUNT_POINTS + ctx.attrs.ops,
        package_sets = ctx.attrs.package_sets,
        packages = ctx.attrs.packages,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
    )
    return declare_portable(
        ctx,
        tools = ctx.attrs._tools[ImageToolsInfo],
        arch = ctx.attrs.arch,
        image = image,
        name = ctx.label.name,
        seed = ctx.attrs.seed,
        verity_key = resolve_signing_key(ctx.attrs.verity_key),
        version = ctx.attrs.version,
    )

_portable = rule(
    impl = _portable_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS | PORTABLE_ATTRS | _ROOT_ATTRS,
)

def _esp_operations(
    esp_files: dict[str, Artifact],
    ukis: Artifact,
    secure_boot_key: SigningKeyInfo | None,
) -> list[LayerOperation]:
    operations = [copy(ukis, "/boot/EFI/Linux")] + install_systemd_boot(secure_boot_key)
    for destination in sorted(esp_files):
        if not (destination.startswith("/boot/") or destination.startswith("/efi/")):
            fail(
                "bootable_disk_image: esp_files destination must be under /boot or /efi, got {!r}".format(
                    destination,
                )
            )
        operations.append(copy(esp_files[destination], destination))
    return operations

def _bootable_disk_image_impl(ctx: AnalysisContext) -> list[Provider]:
    tools = ctx.attrs._tools[ImageToolsInfo]
    if (ctx.attrs.parent == None) == (ctx.attrs.package_manager == None):
        fail("bootable_disk_image: exactly one of parent and package_manager is required")

    image_id = check_name(
        "bootable_disk_image image_id",
        ctx.attrs.image_id or ctx.label.name,
    )

    # The version lands in partition labels and the UKI filename.
    version = check_name("bootable_disk_image version", ctx.attrs.version, VERSION_PATTERN)

    secure_boot_key = resolve_signing_key(ctx.attrs.secure_boot_key)

    initrd_info = ctx.attrs.initrd[InitrdInfo]
    initrd = initrd_info.image

    root = declare_image(
        ctx,
        tools = tools,
        identifier = "root",
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = generated(ctx.attrs.ops, ctx.attrs.packages + ctx.attrs.package_sets),
        package_manager = ctx.attrs.package_manager,
        package_sets = ctx.attrs.package_sets,
        parent = ctx.attrs.parent[ImageInfo] if ctx.attrs.parent != None else None,
        packages = ctx.attrs.packages,
        tmpfiles = ctx.attrs.tmpfiles,
        version = version,
    )

    # The identity stamp lives in its own thin layer so that a changing version only re-runs the
    # artifacts that embed it, never package installation or the caller's operations. systemd-boot
    # signing joins it for the same reason (a key change must not re-run the caller's operations):
    # it must precede the verity partitions below so that the booted /usr carries the signed
    # binary (see sign_systemd_boot).
    identity_ops = [merge_os_release({"IMAGE_ID": image_id, "IMAGE_VERSION": version})]
    if secure_boot_key != None:
        identity_ops += sign_systemd_boot(secure_boot_key, ctx.attrs.arch)
    identity = declare_image(
        ctx,
        tools = tools,
        identifier = "identity",
        keys = [secure_boot_key],
        ops = identity_ops,
        parent = root,
        version = version,
    )

    definitions = format_partition_labels(
        [json.decode(value) for value in ctx.attrs.definitions],
        image_id,
        version,
    )
    system_definitions = []
    boot_definitions = []
    for definition in definitions:
        if definition["type"] == "esp":
            boot_definitions.append(definition)
        else:
            system_definitions.append(definition)
    if not system_definitions or not boot_definitions:
        fail("bootable_disk_image: definitions must include system and ESP partitions")

    # An option set for a filesystem this disk never formats would go nowhere: repart reads the
    # variable named after the filesystem it is about to create and ignores the rest.
    formatted = {definition["filesystem"]: True for definition in definitions if definition["filesystem"]}
    unknown = [name for name in sorted(ctx.attrs.mkfs_options) if name not in formatted]
    if unknown:
        fail(
            "bootable_disk_image: mkfs_options for {}, which no partition of this disk formats; it formats {}".format(
                unknown,
                sorted(formatted),
            )
        )

    # Everything this composition publishes is named after the image identity: the disk, the UKI,
    # and every split partition, which repart names after the image it writes them out of.
    basename = "{}_{}_{}".format(image_id, version, ARCHITECTURES[ctx.attrs.arch].systemd)

    system = declare_ddi(
        ctx,
        tools = tools,
        systemd_arch = ARCHITECTURES[ctx.attrs.arch].systemd,
        basename = basename,
        definitions = system_definitions,
        disk = False,
        identifier = "system",
        image = identity,
        mkfs_options = ctx.attrs.mkfs_options,
        seed = ctx.attrs.disk_seed,
        split = True,
        strip_pkgdb = ctx.attrs.strip_pkgdb,
        verity_key = resolve_signing_key(ctx.attrs.verity_key),
    )

    uki = declare_uki(
        ctx,
        tools = tools,
        arch = ctx.attrs.arch,
        cmdline = ctx.attrs.cmdline,
        identifier = "uki",
        image = identity,
        image_id = image_id,
        initrd_modules = ctx.attrs.initrd_modules,
        initrds = [initrd_info.cpio],
        profiles = ctx.attrs.profiles,
        root_hash = system.info.root_hash,
        secure_boot_key = secure_boot_key,
        sign_expected_pcr_key = resolve_signing_key(ctx.attrs.sign_expected_pcr_key),
        splash = ctx.attrs.splash,
        version = version,
    )
    esp = declare_image(
        ctx,
        tools = tools,
        identifier = "esp",
        keys = [secure_boot_key],
        ops = _esp_operations(ctx.attrs.esp_files, uki.ukis, secure_boot_key),
        parent = identity,
        version = version,
    )

    disk = declare_ddi(
        ctx,
        tools = tools,
        systemd_arch = ARCHITECTURES[ctx.attrs.arch].systemd,
        basename = basename,
        definitions = boot_definitions,
        identifier = "disk",
        image = esp,
        imported = [system.info],
        mkfs_options = ctx.attrs.mkfs_options,
        seed = ctx.attrs.disk_seed,
    )
    raw = disk.info.disk
    directory = declare_image_directory(ctx, tools = tools, identifier = "directory", image = esp)
    conversions = [
        declare_disk_conversion(
            ctx,
            tools = tools,
            basename = basename,
            disk = disk.info,
            box = esp.box,
            format = format,
            virtual_size = ctx.attrs.virtual_size if format in VIRTUAL_SIZE_FORMATS else None,
            identifier = format,
        )
        for format in DISK_FORMATS
    ]

    # What this disk boots, as files rather than as PE sections: a direct kernel boot needs the
    # kernel and the initrd the UKI carries, and publishing needs the one UKI out of the set the
    # image ships. Declared here so a caller does not have to point a second target at the ESP.
    boot = declare_boot_artifacts(ctx, tools = tools, identifier = "boot", image = esp)

    # The system partitions are filled by their own repart run and imported into this one, so what
    # the disk holds is what the two runs between them listed.
    manifests = system.info.manifests | disk.info.manifests

    sub_targets = dict(disk.sub_targets)
    sub_targets.update(manifest_sub_targets(manifests))
    sub_targets.update({
        "boot": [
            DefaultInfo(
                default_outputs = [boot.kernel, boot.initrd],
                sub_targets = boot_subtargets(boot),
            ),
        ],
        "directory": [DefaultInfo(default_output = directory.directory), directory],
        "initrd": [
            DefaultInfo(
                default_output = initrd_info.cpio.archive,
                sub_targets = published_metadata_subtargets(initrd, "{}.initrd".format(basename)),
            ),
            initrd_info,
        ],
        "uki": [DefaultInfo(default_output = uki.ukis, sub_targets = uki_subtargets(uki)), uki],
    })
    sub_targets["ami"] = declare_ami(
        ctx.actions,
        arch = ctx.attrs.arch,
        basename = basename,
        box = ctx.attrs._ami_box[BoxInfo],
        disk = raw,
        importer = ctx.attrs._ami_import[RunInfo],
        # A key held by a token has a URI, not a file, for a certificate; every key rule publishes the
        # file as `[cert]`.
        secure_boot_certificate = (
            ctx.attrs.secure_boot_key[DefaultInfo].sub_targets["cert"][DefaultInfo].default_outputs[0] if secure_boot_key != None else None
        ),
        virtual_size = ctx.attrs.virtual_size,
    )
    for conversion in conversions:
        sub_targets[conversion.format] = [
            DefaultInfo(default_output = conversion.image),
            # A re-encoding is publishable in its own right, so a release can gather the subtarget
            # rather than the disk; it is not in the disk's own set, which would build every
            # encoding whenever anything materializes the release.
            PublishedInfo(artifacts = {conversion.image.basename: conversion.image}),
            conversion,
        ]

    # An update carries the partitions and the UKI; the disk itself is the installation medium, and
    # the kernel and initrd are what boots one without a boot loader. The ESP is none of those: what
    # it holds arrives as transfers of its own, so it is left out rather than published as a
    # partition nothing ever writes back. The listings join them: one per partition, naming what
    # that partition puts on a machine and nothing the image happens to hold besides, and one for
    # the disk, which is what an installation of it leaves behind.
    artifacts = {
        "{}.efi".format(basename): boot.uki,
        "{}.initrd".format(basename): boot.initrd,
        "{}.vmlinuz".format(basename): boot.kernel,
        raw.basename: raw,
    }
    for name, listing in manifests.items():
        artifacts["{}.{}.Uapi16Manifest".format(basename, name)] = listing
    whole = disk.info.manifest
    if whole != None:
        artifacts[whole.basename] = whole

    published = PublishedInfo(
        artifacts = artifacts,
        partitions = [partition for partition in disk.info.partitions if partition.definition["type"] != "esp"],
    )

    return image_providers(
        default_outputs = [raw],
        extra = [directory, disk.info, initrd_info, published, uki],
        image = esp,
        sub_targets = sub_targets | published_metadata_subtargets(esp, basename),
    )

_bootable_disk_image = rule(
    impl = _bootable_disk_image_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS
    | UKI_ATTRS
    | DDI_ATTRS
    | _ROOT_ATTRS
    | {
        "cmdline": attrs.list(
            attrs.string(),
            default = ["root=tmpfs", "mount.usr=dissect", "rw"],
        ),
        "disk_seed": attrs.option(attrs.string(), default = None),
        "esp_files": attrs.dict(
            attrs.string(),
            attrs.source(allow_directory = True),
            default = {},
        ),
        "image_id": attrs.option(attrs.string(), default = None),
        "initrd": attrs.dep(
            providers = [InitrdInfo],
            doc = "the initrd to boot; the declaration macro declares a conventional one when given none",
        ),
        "virtual_size": VIRTUAL_SIZE_ATTR,
        "_ami_box": attrs.exec_dep(providers = [BoxInfo], default = "tine//tools:aws.box"),
        "_ami_import": attrs.exec_dep(providers = [RunInfo], default = "tine//image:ami-import"),
    },
)

def archive(
    name: str,
    ops: list[LayerOperationTree] = [],
    version: str | Select | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    **kwargs,
) -> None:
    """Build one logical image from operations and emit it as an archive."""
    _archive(
        name = name,
        ops = flatten_operations(ops),
        version = resolve_version("archive {}".format(name), version),
        **(distribution.distributed(name, distro, visibility) | kwargs),
    )

def extension(
    name: str,
    ops: list[LayerOperationTree] = [],
    version: str | Select | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    **kwargs,
) -> None:
    """Build one logical image from operations and package it as an extension DDI.

    `classes` lists the extension classes the DDI contains: "sysext" for /usr and /opt, "confext"
    for /etc. With `parent`, the DDI contains only the delta above that image.
    """
    _extension(
        name = name,
        ops = flatten_operations(ops),
        version = resolve_version("extension {}".format(name), version),
        **(distribution.distributed(name, distro, visibility) | kwargs),
    )

def sysext(name: str, **kwargs) -> None:
    """Build a system extension: an extension DDI that contains /usr and /opt."""
    extension(name = name, classes = ["sysext"], **kwargs)

def confext(name: str, **kwargs) -> None:
    """Build a configuration extension: an extension DDI that contains /etc."""
    extension(name = name, classes = ["confext"], **kwargs)

def portable(
    name: str,
    ops: list[LayerOperationTree] = [],
    version: str | Select | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    **kwargs,
) -> None:
    """Build one logical image from operations and package it as a portable service DDI."""
    _portable(
        name = name,
        ops = flatten_operations(ops),
        version = resolve_version("portable {}".format(name), version),
        **(distribution.distributed(name, distro, visibility) | kwargs),
    )

def bootable_disk_image(
    name: str,
    definitions: list[Partition],
    ops: list[LayerOperationTree] = [],
    profiles: list[UkiProfile] = [],
    initrd: str | Select | None = None,
    package_manager: str | Select | None = None,
    image_id: str | None = None,
    version: str | Select | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    **kwargs,
) -> None:
    """Compose the initrd, versioned UKIs, the ESP, and system partitions into a disk.

    Without `initrd`, a conventional `initrd_image()` is declared as `<name>.initrd` and booted;
    declare one yourself to configure its packages, operations, or compression, which a disk
    extending `parent` has to do because the initrd takes its package manager from the disk.

    With secure_boot_key, the UKIs and systemd-boot are signed for Secure Boot, and the ESP receives
    key auto-enrollment files for firmware in setup mode. With sign_expected_pcr_key, the UKIs carry
    a signed expected-PCR policy.
    """
    # Resolved once, against the labels this disk renders, and passed on: the initrd below carries
    # the version of the disk it boots rather than one rendered against its own empty budget.
    version = resolve_version(
        "bootable_disk_image {}".format(name),
        version,
        labels = [d["label"] for d in definitions if d["label"] != None],
        image_id = image_id or name,
    )

    if initrd == None:
        if package_manager == None:
            fail(
                "bootable_disk_image {}: declare an `initrd_image()` and pass it as `initrd`; ".format(name)
                + "a disk extending `parent` names no package manager to declare one from",
            )
        initrd = ":{}.initrd".format(name)
        initrd_image(
            name = name + ".initrd",
            package_manager = package_manager,
            version = version,
            visibility = visibility,
            # The image naming a distribution of its own makes its initrd that distribution too;
            # otherwise the leaf pulling it in decides, exactly as a parent chain does.
            distro = distro,
        )
    _bootable_disk_image(
        name = name,
        image_id = image_id,
        initrd = initrd,
        # The rule renders label placeholders from its own identity during analysis.
        definitions = [json.encode(definition) for definition in definitions],
        ops = flatten_operations(ops),
        package_manager = package_manager,
        profiles = encode_profiles(profiles),
        version = version,
        **(distribution.distributed(name, distro, visibility) | kwargs),
    )
