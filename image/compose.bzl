"""Convenience compositions for image products."""

load("//distribution:defs.bzl", "distribution_aliases", "distribution_attrs")
load(
    "//image_format:archive.bzl",
    "ARCHIVE_ATTRS",
    "declare_image_archive",
    "declare_image_directory",
)
load("//image_format:boot.bzl", "boot_subtargets", "declare_boot_artifacts")
load(
    "//image_format:disk.bzl",
    "DISK_FORMATS",
    "Partition",  # @unused Used as a type.
    "declare_disk_conversion",
    "declare_repart",
    "encode_definitions",
    "format_partition_labels",
)
load("//image_format:sysext.bzl", "SYSEXT_ATTRS", "declare_image_sysext")
load(
    "//image_format:uki.bzl",
    "UKI_ATTRS",
    "UkiProfile",  # @unused Used as a type.
    "declare_uki",
    "encode_profiles",
    "uki_subtargets",
)
load("//package:manager.bzl", "PackageManagerInfo")
load(
    ":image.bzl",
    "ARCHES",
    "IMAGE_ATTRS",
    "ImageInfo",
    "LayerOperation",  # @unused Used as a type.
    "LayerOperationTree",  # @unused Used as a type.
    "VERSION_PATTERN",
    "check_name",
    "copy",
    "declare_image",
    "flatten_operations",
    "generated",
    "image_metadata_subtargets",
    "image_providers",
    "install_systemd_boot",
    "merge_os_release",
    "sign_systemd_boot",
)
load(":initrd.bzl", "InitrdInfo", "initrd_image")
load(":publish.bzl", "PublishedInfo")
load(
    ":sign.bzl",
    "SigningKeyInfo",  # @unused Used as a function argument type.
    "VERITY_KEY_ATTR",
    "resolve_signing_key",
)

def _composed_image(
    ctx: AnalysisContext,
    generate: bool = True,
    package_manager: Dependency | None = None,
    parent: ImageInfo | None = None,
) -> ImageInfo:
    return declare_image(
        ctx,
        identifier = "image",
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = generated(ctx.attrs.ops, ctx.attrs.packages + ctx.attrs.package_sets) if generate else ctx.attrs.ops,
        package_manager = package_manager,
        package_sets = ctx.attrs.package_sets,
        packages = ctx.attrs.packages,
        parent = parent,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
    )

def _rootfs_archive_impl(ctx: AnalysisContext) -> list[Provider]:
    image = _composed_image(ctx, package_manager = ctx.attrs.package_manager)
    archive = declare_image_archive(
        ctx,
        compression = ctx.attrs.compression,
        format = ctx.attrs.format,
        image = image,
    )
    return image_providers(
        default_outputs = [archive.archive],
        extra = [archive],
        image = image,
    )

_rootfs_archive = rule(
    impl = _rootfs_archive_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS
    | ARCHIVE_ATTRS
    | {
        "package_manager": attrs.dep(providers = [PackageManagerInfo]),
    },
)

def _sysext_image_impl(ctx: AnalysisContext) -> list[Provider]:
    if (ctx.attrs.base == None) == (ctx.attrs.package_manager == None):
        fail("sysext_image: exactly one of base and package_manager is required")

    # An extension merges onto a system it does not own, so it generates nothing: a database built
    # from its own tree would shadow that system's while describing only what the extension carries,
    # exactly as its package database would.
    base = ctx.attrs.base[ImageInfo] if ctx.attrs.base != None else None
    if base != None:
        image = _composed_image(ctx, generate = False, parent = base)
    else:
        image = _composed_image(ctx, generate = False, package_manager = ctx.attrs.package_manager)

    sysext = declare_image_sysext(
        ctx,
        arch = ctx.attrs.arch,
        base = base,
        extension = ctx.label.name,
        image = image,
        release = ctx.attrs.release,
        seed = ctx.attrs.seed,
        verity_key = resolve_signing_key(ctx.attrs.verity_key),
        version = ctx.attrs.version,
    )
    return image_providers(
        default_outputs = [sysext.image],
        extra = [sysext, PublishedInfo(artifacts = {sysext.image.basename: sysext.image})],
        image = image,
    )

_sysext_image = rule(
    impl = _sysext_image_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS
    | SYSEXT_ATTRS
    | {
        "base": attrs.option(attrs.dep(providers = [ImageInfo]), default = None),
        "package_manager": attrs.option(
            attrs.dep(providers = [PackageManagerInfo]),
            default = None,
        ),
    },
)

def _esp_operations(
    ctx: AnalysisContext,
    ukis: Artifact,
    secure_boot_key: SigningKeyInfo | None,
) -> list[LayerOperation]:
    operations = [copy(ukis, "/boot/EFI/Linux")] + install_systemd_boot(secure_boot_key)
    for destination in sorted(ctx.attrs.esp_files):
        if not (destination.startswith("/boot/") or destination.startswith("/efi/")):
            fail(
                "bootable_disk_image: esp_files destination must be under /boot or /efi, got {!r}".format(
                    destination,
                )
            )
        operations.append(copy(ctx.attrs.esp_files[destination], destination))
    return operations

def _bootable_disk_image_impl(ctx: AnalysisContext) -> list[Provider]:
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
    verity = False
    for definition in definitions:
        if definition["type"] == "esp":
            boot_definitions.append(json.encode(definition))
        else:
            system_definitions.append(json.encode(definition))
            verity = verity or definition["verity"] == "data"
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
    basename = "{}_{}_{}".format(image_id, version, ARCHES[ctx.attrs.arch].systemd)

    system = declare_repart(
        ctx,
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
        arch = ctx.attrs.arch,
        cmdline = ctx.attrs.cmdline,
        identifier = "uki",
        image = identity,
        image_id = image_id,
        initrd_modules = ctx.attrs.initrd_modules,
        initrds = [initrd_info.cpio],
        profiles = ctx.attrs.profiles,
        root_hash = system.info.root_hash if verity else None,
        secure_boot_key = secure_boot_key,
        sign_expected_pcr_key = resolve_signing_key(ctx.attrs.sign_expected_pcr_key),
        version = version,
    )
    esp = declare_image(
        ctx,
        identifier = "esp",
        keys = [secure_boot_key],
        ops = _esp_operations(ctx, uki.ukis, secure_boot_key),
        parent = identity,
        version = version,
    )

    disk = declare_repart(
        ctx,
        basename = basename,
        definitions = boot_definitions,
        identifier = "disk",
        image = esp,
        imported = [system.info],
        imported_root_hash = system.info.root_hash,
        mkfs_options = ctx.attrs.mkfs_options,
        output_size = ctx.attrs.output_size,
        seed = ctx.attrs.disk_seed,
    )
    raw = disk.info.disk
    if raw == None:
        fail("bootable_disk_image: internal disk repart did not expose a composed disk")
    directory = declare_image_directory(ctx, identifier = "directory", image = esp)
    conversions = [
        declare_disk_conversion(
            ctx,
            basename = basename,
            disk = disk.info,
            engine = esp.engine,
            format = format,
            identifier = format,
        )
        for format in DISK_FORMATS
    ]

    # What this disk boots, as files rather than as PE sections: a direct kernel boot needs the
    # kernel and the initrd the UKI carries, and publishing needs the one UKI out of the set the
    # image ships. Declared here so a caller does not have to point a second target at the ESP.
    boot = declare_boot_artifacts(ctx, identifier = "boot", image = esp)

    sub_targets = dict(disk.sub_targets)
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
                sub_targets = image_metadata_subtargets(initrd),
            ),
            initrd_info,
        ],
        "uki": [DefaultInfo(default_output = uki.ukis, sub_targets = uki_subtargets(uki)), uki],
    })
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
    # partition nothing ever writes back.
    published = PublishedInfo(
        artifacts = {
            "{}.efi".format(basename): boot.uki,
            "{}.initrd".format(basename): boot.initrd,
            "{}.vmlinuz".format(basename): boot.kernel,
            raw.basename: raw,
        },
        partitions = [partition for partition in disk.info.partitions if partition.definition["type"] != "esp"],
    )

    return image_providers(
        default_outputs = [raw],
        extra = [directory, disk.info, initrd_info, published, uki],
        image = esp,
        sub_targets = sub_targets,
    )

_bootable_disk_image = rule(
    impl = _bootable_disk_image_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS
    | UKI_ATTRS
    | {
        "cmdline": attrs.list(
            attrs.string(),
            default = ["root=tmpfs", "mount.usr=dissect", "rw"],
        ),
        "definitions": attrs.list(attrs.string(), doc = "serialized partition definitions"),
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
        "mkfs_options": attrs.dict(
            attrs.string(),
            attrs.list(attrs.string()),
            default = {},
            doc = "filesystem -> the options mkfs is given when formatting a partition of that type",
        ),
        "output_size": attrs.option(
            attrs.string(),
            default = None,
            doc = 'size to compose the disk at, e.g. "20G"; the room past the partitions stays free',
        ),
        "package_manager": attrs.option(
            attrs.dep(providers = [PackageManagerInfo]),
            default = None,
        ),
        "parent": attrs.option(
            attrs.dep(providers = [ImageInfo]),
            default = None,
            doc = "the logical image the disk extends instead of starting one of its own",
        ),
        "strip_pkgdb": attrs.bool(
            default = False,
            doc = "leave the package database out of the system partitions; the image still carries it",
        ),
        "verity_key": VERITY_KEY_ATTR,
    },
)

def rootfs_archive(
    name: str,
    ops: list[LayerOperationTree] = [],
    distribution: str | None = None,
    visibility: list[str] | None = None,
    **kwargs,
) -> None:
    """Build one logical image from operations and emit it as an archive."""
    distribution_aliases(name, distribution, visibility)
    _rootfs_archive(name = name, ops = flatten_operations(ops), **(distribution_attrs(distribution, visibility) | kwargs))

def sysext_image(
    name: str,
    ops: list[LayerOperationTree] = [],
    distribution: str | None = None,
    visibility: list[str] | None = None,
    **kwargs,
) -> None:
    """Build one logical image from operations and package it as a system-extension DDI."""
    distribution_aliases(name, distribution, visibility)
    _sysext_image(name = name, ops = flatten_operations(ops), **(distribution_attrs(distribution, visibility) | kwargs))

def bootable_disk_image(
    name: str,
    definitions: list[Partition],
    ops: list[LayerOperationTree] = [],
    profiles: list[UkiProfile] = [],
    verity_key: str | None = None,
    secure_boot_key: str | None = None,
    sign_expected_pcr_key: str | None = None,
    initrd: str | Select | None = None,
    package_manager: str | Select | None = None,
    version: str | Select | None = None,
    distribution: str | None = None,
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
            distribution = distribution,
        )
    distribution_aliases(name, distribution, visibility)
    _bootable_disk_image(
        name = name,
        initrd = initrd,
        # The rule renders label placeholders from its own identity during analysis.
        definitions = encode_definitions(
            definitions,
            disk = True,
            imports = False,
            rendered = False,
            signed = verity_key != None,
            split = True,
        ),
        ops = flatten_operations(ops),
        package_manager = package_manager,
        profiles = encode_profiles(profiles),
        secure_boot_key = secure_boot_key,
        sign_expected_pcr_key = sign_expected_pcr_key,
        verity_key = verity_key,
        version = version,
        **(distribution_attrs(distribution, visibility) | kwargs),
    )
