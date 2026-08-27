"""The public image API.

Load the `image` namespace from here; the modules behind it are implementation structure and may be
rearranged.
"""

load(
    "//image_format:archive.bzl",
    "COMPRESSIONS",
    "ImageArchiveInfo",
    "ImageDirectoryInfo",
    "image_archive",
    "image_directory",
)
load("//image_format:boot.bzl", "bootable")
load(
    "//image_format:disk.bzl",
    "DEFAULT_ROOT_PARTITIONS",
    "DEFAULT_SIGNED_USR_VERITY_PARTITIONS",
    "DEFAULT_USR_VERITY_PARTITIONS",
    "DISK_FORMATS",
    "DiskConversionInfo",
    "RepartInfo",
    "RootHashInfo",
    "disk_convert",
    "format_partition_labels",
    "partition",
    "repart",
)
load("//image_format:modules.bzl", "DEFAULT_INITRD_MODULES")
load(
    "//image_format:sysext.bzl",
    "SysextImageInfo",
    "image_sysext",
)
load(
    "//image_format:uki.bzl",
    "UkiInfo",
    "uki",
    "uki_profile",
)
load(
    ":compose.bzl",
    "bootable_disk_image",
    "rootfs_archive",
    "sysext_image",
)
load(
    ":image.bzl",
    "ImageInfo",
    "ImageInstallInfo",
    "ImageSbomInfo",
    "copy",
    "depmod",
    "hwdb",
    "install_from",
    "install_systemd_boot",
    "layer",
    "locale_gen",
    "merge_os_release",
    "mkdir",
    "python",
    "remove",
    "run",
    "symlink",
    "write_file",
)
load(":initrd.bzl", "InitrdInfo", "initrd_image")
load(":install.bzl", "image_install")
load(":publish.bzl", "PublishedInfo", "image_artifacts")
load(
    ":sign.bzl",
    "SigningKeyInfo",
    "generate_signing_key",
    "pem_signing_key",
    "pkcs11_signing_key",
)
load(":substitute.bzl", "substitute")
load(":version.bzl", "resolve_version")
load(":vm.bzl", "image_vm")

image = struct(
    # Logical images and their operations.
    layer = layer,
    run = run,
    python = python,
    install_from = install_from,
    mkdir = mkdir,
    symlink = symlink,
    write_file = write_file,
    remove = remove,
    copy = copy,
    merge_os_release = merge_os_release,
    install_systemd_boot = install_systemd_boot,
    depmod = depmod,
    hwdb = hwdb,
    locale_gen = locale_gen,
    # Terminal outputs.
    archive = image_archive,
    directory = image_directory,
    sysext = image_sysext,
    uki = uki,
    uki_profile = uki_profile,
    resolve_version = resolve_version,
    bootable = bootable,
    repart = repart,
    partition = partition,
    format_partition_labels = format_partition_labels,
    disk_convert = disk_convert,
    vm = image_vm,
    generate_signing_key = generate_signing_key,
    install = image_install,
    substitute = substitute,
    pem_signing_key = pem_signing_key,
    pkcs11_signing_key = pkcs11_signing_key,
    SigningKeyInfo = SigningKeyInfo,
    # Compositions.
    rootfs_archive = rootfs_archive,
    sysext_image = sysext_image,
    bootable_disk = bootable_disk_image,
    artifacts = image_artifacts,
    initrd = initrd_image,
    # The core kernel modules a UKI carries unless a target says otherwise; extend it with `+`.
    DEFAULT_INITRD_MODULES = DEFAULT_INITRD_MODULES,
    # Conventional partition layouts.
    DEFAULT_ROOT_PARTITIONS = DEFAULT_ROOT_PARTITIONS,
    DEFAULT_USR_VERITY_PARTITIONS = DEFAULT_USR_VERITY_PARTITIONS,
    DEFAULT_SIGNED_USR_VERITY_PARTITIONS = DEFAULT_SIGNED_USR_VERITY_PARTITIONS,
    COMPRESSIONS = COMPRESSIONS,
    DISK_FORMATS = DISK_FORMATS,
    # Providers.
    ImageInfo = ImageInfo,
    ImageInstallInfo = ImageInstallInfo,
    ImageSbomInfo = ImageSbomInfo,
    ImageArchiveInfo = ImageArchiveInfo,
    ImageDirectoryInfo = ImageDirectoryInfo,
    SysextImageInfo = SysextImageInfo,
    UkiInfo = UkiInfo,
    RepartInfo = RepartInfo,
    RootHashInfo = RootHashInfo,
    DiskConversionInfo = DiskConversionInfo,
    InitrdInfo = InitrdInfo,
    PublishedInfo = PublishedInfo,
)
