"""Convenience compositions for image products."""

load(
    "//image_format:archive.bzl",
    "ARCHIVE_ATTRS",
    "ImageArchiveInfo",
    "declare_image_archive",
    "declare_image_directory",
)
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
    "image_metadata_subtargets",
    "image_providers",
    "install_package_set",
    "install_systemd_boot",
    "merge_os_release",
    "remove",
    "sign_systemd_boot",
    "symlink",
)

InitrdInfo = provider(
    doc = "A logical initrd image and its derived cpio archive.",
    fields = {
        "cpio": provider_field(ImageArchiveInfo),
        "image": provider_field(ImageInfo),
    },
)

def _composed_image(ctx: AnalysisContext, **kwargs) -> ImageInfo:
    return declare_image(
        ctx,
        identifier = "image",
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = ctx.attrs.ops,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
        **kwargs
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
    attrs = IMAGE_ATTRS | ARCHIVE_ATTRS | {
        "package_manager": attrs.dep(providers = [PackageManagerInfo]),
    },
)

def _sysext_image_impl(ctx: AnalysisContext) -> list[Provider]:
    if (ctx.attrs.base == None) == (ctx.attrs.package_manager == None):
        fail("sysext_image: exactly one of base and package_manager is required")

    base = ctx.attrs.base[ImageInfo] if ctx.attrs.base != None else None
    if base != None:
        image = _composed_image(ctx, parent = base)
    else:
        image = _composed_image(ctx, package_manager = ctx.attrs.package_manager)

    sysext = declare_image_sysext(
        ctx,
        base = base,
        extension = ctx.label.name,
        image = image,
        release = ctx.attrs.release,
        seed = ctx.attrs.seed,
    )
    return image_providers(
        default_outputs = [sysext.image],
        extra = [sysext],
        image = image,
    )

_sysext_image = rule(
    impl = _sysext_image_impl,
    attrs = IMAGE_ATTRS | SYSEXT_ATTRS | {
        "base": attrs.option(attrs.dep(providers = [ImageInfo]), default = None),
        "package_manager": attrs.option(
            attrs.dep(providers = [PackageManagerInfo]),
            default = None,
        ),
    },
)

_DEFAULT_INITRD_OPS = [
    install_package_set("initrd"),
    symlink("/usr/lib/systemd/systemd", "/init"),
    symlink("/etc/os-release", "/etc/initrd-release"),
    # udev reads its binary hardware database at run time, not the sources installed with it.
    remove("/usr/lib/udev/hwdb.d"),
    # These databases serve humans and service-name resolution, neither of which happens in the initrd.
    remove("/usr/lib/systemd/catalog"),
    remove("/var/lib/systemd/catalog"),
    remove("/etc/services"),
]

def _esp_operations(ctx: AnalysisContext, ukis: Artifact) -> list[LayerOperation]:
    operations = [copy(ukis, "/boot/EFI/Linux")] + install_systemd_boot(
        certificate = ctx.attrs.secure_boot_certificate,
        private_key = ctx.attrs.secure_boot_private_key,
    )
    for destination in sorted(ctx.attrs.esp_files):
        if not (destination.startswith("/boot/") or destination.startswith("/efi/")):
            fail("bootable_disk_image: esp_files destination must be under /boot or /efi, got {!r}".format(
                destination,
            ))
        operations.append(copy(ctx.attrs.esp_files[destination], destination))
    return operations

def _bootable_disk_image_impl(ctx: AnalysisContext) -> list[Provider]:
    image_id = check_name(
        "bootable_disk_image image_id",
        ctx.attrs.image_id or ctx.label.name,
    )

    # The version lands in partition labels and the UKI filename.
    version = check_name("bootable_disk_image version", ctx.attrs.version, VERSION_PATTERN)

    if ctx.attrs.initrd != None:
        initrd = ctx.attrs.initrd[ImageInfo]
    else:
        initrd = declare_image(
            ctx,
            identifier = "initrd",
            install_docs = False,
            install_langs = ["C.UTF-8"],
            ops = _DEFAULT_INITRD_OPS,
            package_manager = ctx.attrs.package_manager,
            source_name = ctx.label.name + ".initrd",
            version = version,
        )

    root = declare_image(
        ctx,
        identifier = "root",
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = ctx.attrs.ops,
        package_manager = ctx.attrs.package_manager,
        tmpfiles = ctx.attrs.tmpfiles,
        version = version,
    )

    # The identity stamp lives in its own thin layer so that a changing version only re-runs the
    # artifacts that embed it, never package installation or the caller's operations. systemd-boot
    # signing joins it for the same reason (a key change must not re-run the caller's operations):
    # it must precede the verity partitions below so that the booted /usr carries the signed
    # binary (see sign_systemd_boot).
    identity_ops = [merge_os_release({"IMAGE_ID": image_id, "IMAGE_VERSION": version})]
    if ctx.attrs.secure_boot_private_key != None:
        identity_ops += sign_systemd_boot(
            ctx.attrs.secure_boot_private_key,
            ctx.attrs.secure_boot_certificate,
            ctx.attrs.arch,
        )
    identity = declare_image(
        ctx,
        identifier = "identity",
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

    system = declare_repart(
        ctx,
        certificate = ctx.attrs.verity_certificate,
        definitions = system_definitions,
        disk = False,
        identifier = "system",
        image = identity,
        private_key = ctx.attrs.verity_private_key,
        seed = ctx.attrs.disk_seed,
        split = True,
    )

    # An initrd never resolves a dependency or verifies a package, and the kernel unpacks the whole
    # cpio into tmpfs, so the package database only costs boot memory here.
    initrd_archive = declare_image_archive(
        ctx,
        compression = "zstd",
        format = "cpio",
        identifier = "initrd",
        image = initrd,
        strip_pkgdb = True,
    )
    uki = declare_uki(
        ctx,
        arch = ctx.attrs.arch,
        cmdline = ctx.attrs.cmdline,
        identifier = "uki",
        image = identity,
        image_id = image_id,
        initrds = [initrd_archive],
        profiles = ctx.attrs.profiles,
        root_hash = system.info.root_hash if verity else None,
        secure_boot_certificate = ctx.attrs.secure_boot_certificate,
        secure_boot_private_key = ctx.attrs.secure_boot_private_key,
        version = version,
    )
    esp = declare_image(
        ctx,
        identifier = "esp",
        ops = _esp_operations(ctx, uki.ukis),
        parent = identity,
        version = version,
    )

    # The disk file leaves the build as an update or installation medium, so it carries the image
    # identity in its name, exactly like the UKI.
    basename = "{}_{}_{}".format(image_id, version, ARCHES[ctx.attrs.arch].systemd)
    disk = declare_repart(
        ctx,
        basename = basename,
        definitions = boot_definitions,
        identifier = "disk",
        image = esp,
        imported = [system.info],
        imported_root_hash = system.info.root_hash,
        seed = ctx.attrs.disk_seed,
        split = True,
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

    initrd_info = InitrdInfo(cpio = initrd_archive, image = initrd)
    sub_targets = dict(disk.sub_targets)
    sub_targets.update({
        "directory": [DefaultInfo(default_output = directory.directory), directory],
        "initrd": [
            DefaultInfo(
                default_output = initrd_archive.archive,
                sub_targets = image_metadata_subtargets(initrd),
            ),
            initrd_info,
        ],
        "uki": [DefaultInfo(default_output = uki.ukis), uki],
    })
    for conversion in conversions:
        sub_targets[conversion.format] = [
            DefaultInfo(default_output = conversion.image),
            conversion,
        ]

    return image_providers(
        default_outputs = [raw],
        extra = [directory, disk.info, initrd_info, uki],
        image = esp,
        sub_targets = sub_targets,
    )

_bootable_disk_image = rule(
    impl = _bootable_disk_image_impl,
    attrs = IMAGE_ATTRS | UKI_ATTRS | {
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
        "initrd": attrs.option(
            attrs.dep(providers = [ImageInfo]),
            default = None,
            doc = (
                "logical image to archive and use as the initrd; " +
                "defaults to the release initrd package set"
            ),
        ),
        "package_manager": attrs.dep(providers = [PackageManagerInfo]),
        "verity_certificate": attrs.option(attrs.source(), default = None),
        "verity_private_key": attrs.option(attrs.source(), default = None),
    },
)

def rootfs_archive(name: str, ops: list[LayerOperationTree] = [], **kwargs) -> None:
    """Build one logical image from operations and emit it as an archive."""
    _rootfs_archive(
        name = name,
        ops = flatten_operations(ops),
        **kwargs
    )

def sysext_image(name: str, ops: list[LayerOperationTree] = [], **kwargs) -> None:
    """Build one logical image from operations and package it as a system-extension DDI."""
    _sysext_image(
        name = name,
        ops = flatten_operations(ops),
        **kwargs
    )

# buildifier: disable=function-docstring-args
def bootable_disk_image(
        name: str,
        definitions: list[Partition],
        ops: list[LayerOperationTree] = [],
        profiles: list[UkiProfile] = [],
        verity_private_key: str | None = None,
        verity_certificate: str | None = None,
        secure_boot_private_key: str | None = None,
        secure_boot_certificate: str | None = None,
        **kwargs) -> None:
    """Compose the default initrd, versioned UKIs, the ESP, and system partitions into a disk.

    With secure_boot_private_key/_certificate, the UKIs and systemd-boot are signed for Secure
    Boot, the UKIs carry a signed expected-PCR policy, and the ESP receives key auto-enrollment
    files for firmware in setup mode.
    """
    if (verity_private_key == None) != (verity_certificate == None):
        fail("bootable_disk_image: verity_private_key and verity_certificate must be specified together")
    if (secure_boot_private_key == None) != (secure_boot_certificate == None):
        fail("bootable_disk_image: secure_boot_private_key and secure_boot_certificate must be specified together")
    _bootable_disk_image(
        name = name,
        # The rule renders label placeholders from its own identity during analysis.
        definitions = encode_definitions(
            definitions,
            disk = True,
            imports = False,
            rendered = False,
            signed = verity_private_key != None,
            split = True,
        ),
        ops = flatten_operations(ops),
        profiles = encode_profiles(profiles),
        secure_boot_certificate = secure_boot_certificate,
        secure_boot_private_key = secure_boot_private_key,
        verity_certificate = verity_certificate,
        verity_private_key = verity_private_key,
        **kwargs
    )
