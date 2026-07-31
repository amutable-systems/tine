"""The public image API.

Load images, operations, terminal outputs, and compositions from here; the modules behind it are
implementation structure and may be rearranged.
"""

load(
    "//image_format:archive.bzl",
    _COMPRESSIONS = "COMPRESSIONS",
    _ImageArchiveInfo = "ImageArchiveInfo",
    _ImageDirectoryInfo = "ImageDirectoryInfo",
    _image_archive = "image_archive",
    _image_directory = "image_directory",
)
load("//image_format:boot.bzl", _bootable = "bootable")
load(
    "//image_format:disk.bzl",
    _DEFAULT_ROOT_PARTITIONS = "DEFAULT_ROOT_PARTITIONS",
    _DEFAULT_SIGNED_USR_VERITY_PARTITIONS = "DEFAULT_SIGNED_USR_VERITY_PARTITIONS",
    _DEFAULT_USR_VERITY_PARTITIONS = "DEFAULT_USR_VERITY_PARTITIONS",
    _DISK_FORMATS = "DISK_FORMATS",
    _DiskConversionInfo = "DiskConversionInfo",
    _RepartInfo = "RepartInfo",
    _RootHashInfo = "RootHashInfo",
    _disk_convert = "disk_convert",
    _format_partition_labels = "format_partition_labels",
    _partition = "partition",
    _repart = "repart",
)
load(
    "//image_format:sysext.bzl",
    _SysextImageInfo = "SysextImageInfo",
    _image_sysext = "image_sysext",
)
load(
    "//image_format:uki.bzl",
    _UkiInfo = "UkiInfo",
    _uki = "uki",
    _uki_profile = "uki_profile",
)
load(
    ":compose.bzl",
    _InitrdInfo = "InitrdInfo",
    _bootable_disk_image = "bootable_disk_image",
    _rootfs_archive = "rootfs_archive",
    _sysext_image = "sysext_image",
)
load(
    ":image.bzl",
    _ImageInfo = "ImageInfo",
    _ImageSbomInfo = "ImageSbomInfo",
    _copy = "copy",
    _image = "image",
    _install = "install",
    _install_package_set = "install_package_set",
    _install_systemd_boot = "install_systemd_boot",
    _merge_os_release = "merge_os_release",
    _mkdir = "mkdir",
    _python = "python",
    _remove = "remove",
    _run = "run",
    _symlink = "symlink",
)
load(":sign.bzl", _signing_key = "signing_key")
load(":vm.bzl", _image_vm = "image_vm")

# Logical images and their operations.
image = _image
run = _run
python = _python
install = _install
install_package_set = _install_package_set
mkdir = _mkdir
symlink = _symlink
remove = _remove
copy = _copy
merge_os_release = _merge_os_release
install_systemd_boot = _install_systemd_boot

# Terminal outputs.
image_archive = _image_archive
image_directory = _image_directory
image_sysext = _image_sysext
uki = _uki
uki_profile = _uki_profile
bootable = _bootable
repart = _repart
partition = _partition
format_partition_labels = _format_partition_labels
disk_convert = _disk_convert
image_vm = _image_vm
signing_key = _signing_key

# Compositions.
rootfs_archive = _rootfs_archive
sysext_image = _sysext_image
bootable_disk_image = _bootable_disk_image

# Conventional partition layouts.
DEFAULT_ROOT_PARTITIONS = _DEFAULT_ROOT_PARTITIONS
DEFAULT_USR_VERITY_PARTITIONS = _DEFAULT_USR_VERITY_PARTITIONS
DEFAULT_SIGNED_USR_VERITY_PARTITIONS = _DEFAULT_SIGNED_USR_VERITY_PARTITIONS
COMPRESSIONS = _COMPRESSIONS
DISK_FORMATS = _DISK_FORMATS

# Providers.
ImageInfo = _ImageInfo
ImageSbomInfo = _ImageSbomInfo
ImageArchiveInfo = _ImageArchiveInfo
ImageDirectoryInfo = _ImageDirectoryInfo
SysextImageInfo = _SysextImageInfo
UkiInfo = _UkiInfo
RepartInfo = _RepartInfo
RootHashInfo = _RootHashInfo
DiskConversionInfo = _DiskConversionInfo
InitrdInfo = _InitrdInfo
