"""Convenience macros for common image compositions."""

load("//image_format:archive.bzl", "image_archive", "image_directory")
load(
    "//image_format:disk.bzl",
    "DISK_FORMATS",
    "Partition",  # @unused Used as a type.
    "disk_convert",
    "format_partition_labels",
    "repart",
)
load("//image_format:pkgdb.bzl", "image_pkgdb")
load("//image_format:sbom.bzl", "image_sbom")
load("//image_format:sysext.bzl", "image_sysext")
load(
    ":boot.bzl",
    "UkiProfile",  # @unused Used as a type.
    "install_systemd_boot",
    "uki",
)
load(
    ":layer.bzl",
    "LayerOperationTree",  # @unused Used as a type.
    "copy",
    "image",
    "image_layer",
    "install_package_set",
    "merge_os_release",
    "remove",
    "symlink",
)

def rootfs_archive(
        name: str,
        package_manager: str,
        ops: list[LayerOperationTree],
        tmpfiles: list[str] = [],
        format: str = "tar",
        compression: str = "none",
        install_docs: bool = True,
        version: str = "0",
        visibility: list[str] | None = None) -> None:
    """Build a root filesystem from ordered operations and archive it."""
    image(
        name = name + ".image",
        package_manager = package_manager,
    )
    image_layer(
        name = name + ".layer",
        parent = ":" + name + ".image",
        ops = ops,
        tmpfiles = tmpfiles,
        install_docs = install_docs,
    )
    image_pkgdb(
        name = name + ".pkgdb",
        image = ":" + name + ".layer",
        visibility = visibility,
    )
    image_sbom(
        name = name + ".sbom",
        image = ":" + name + ".layer",
        source_name = name,
        version = version,
        visibility = visibility,
    )
    image_archive(
        name = name,
        image = ":" + name + ".layer",
        format = format,
        compression = compression,
        visibility = visibility,
    )

def sysext_image(
        name: str,
        ops: list[LayerOperationTree],
        package_manager: str | None = None,
        base: str | None = None,
        tmpfiles: list[str] = [],
        release: dict[str, str] = {},
        seed: str | None = None,
        install_docs: bool = True,
        version: str = "0",
        visibility: list[str] | None = None) -> None:
    """Build a systemd system-extension DDI from ordered operations.

    With `base`, the operations layer on top of that image and only the delta is packaged;
    the extension-release then pins the base's ID/VERSION_ID. With `package_manager`, the
    extension is self-contained and matches any host.
    """
    if (base == None) == (package_manager == None):
        fail("sysext_image: exactly one of base and package_manager is required")
    if base == None:
        image(
            name = name + ".image",
            package_manager = package_manager,
        )
        parent = ":" + name + ".image"
    else:
        parent = base
    image_layer(
        name = name + ".layer",
        parent = parent,
        ops = ops,
        tmpfiles = tmpfiles,
        install_docs = install_docs,
    )
    image_pkgdb(
        name = name + ".pkgdb",
        image = ":" + name + ".layer",
        visibility = visibility,
    )
    image_sbom(
        name = name + ".sbom",
        image = ":" + name + ".layer",
        source_name = name,
        version = version,
        visibility = visibility,
    )
    image_sysext(
        name = name,
        image = ":" + name + ".layer",
        base = base,
        release = release,
        seed = seed,
        visibility = visibility,
    )

# buildifier: disable=function-docstring-args
def _default_initrd(name: str, image: str) -> str:
    image_layer(
        name = name + ".initrd.layer",
        parent = image,
        ops = [
            install_package_set("initrd"),
            symlink("/usr/lib/systemd/systemd", "/init"),
            symlink("/etc/os-release", "/etc/initrd-release"),
            # udev reads its binary hardware database at run time, not the sources it was
            # generated from during installation.
            remove("/usr/lib/udev/hwdb.d"),
            # A journal catalog only matters where a journal is read, and that is the booted system.
            remove("/usr/lib/systemd/catalog"),
            remove("/var/lib/systemd/catalog"),
            # Nothing in an initrd resolves a service name.
            remove("/etc/services"),
        ],
        # Nothing in an initrd is ever read by a human, so neither documentation nor translations
        # are worth carrying.
        install_docs = False,
        install_langs = ["C.UTF-8"],
    )
    return ":" + name + ".initrd.layer"

# buildifier: disable=function-docstring-args
def bootable_disk_image(
        name: str,
        package_manager: str,
        ops: list[LayerOperationTree],
        definitions: list[Partition],
        tmpfiles: list[str] = [],
        initrd: str | None = None,
        cmdline: list[str] = ["root=tmpfs", "mount.usr=dissect", "rw"],
        profiles: list[UkiProfile] = [],
        arch: str = "x86_64",
        disk_seed: str | None = None,
        verity_private_key: str | None = None,
        verity_certificate: str | None = None,
        esp_files: dict[str, str] = {},
        install_docs: bool = True,
        image_id: str | None = None,
        version: str = "0",
        visibility: list[str] | None = None) -> None:
    """Build a UKI-based, systemd-boot GPT disk image."""
    if image_id == None:
        image_id = name
    if not regex_match("^[a-zA-Z0-9._-]+$", image_id):
        fail("bootable_disk_image: invalid image_id {!r}".format(image_id))

    # The version lands in partition labels and the UKI filename; "+" would collide with
    # sd-boot's boot-counting suffixes, "~" is systemd's pre-release separator.
    if not regex_match("^[a-zA-Z0-9._~-]+$", version):
        fail("bootable_disk_image: invalid version {!r}".format(version))
    image(
        name = name + ".image",
        package_manager = package_manager,
    )
    if initrd == None:
        initrd = _default_initrd(name, ":" + name + ".image")

    # The composition owns the cpio so that the supply-chain siblings can scan the initrd (see design.md).
    image_archive(
        name = name + ".initrd",
        image = initrd,
        format = "cpio",
        compression = "zstd",
    )
    image_layer(
        name = name + ".layer",
        parent = ":" + name + ".image",
        ops = ops,
        tmpfiles = tmpfiles,
        install_docs = install_docs,
    )

    # The identity stamp lives in its own thin layer so that a changing version only re-runs
    # the artifacts that embed it, never package installation or the caller's operations.
    image_layer(
        name = name + ".identity.layer",
        parent = ":" + name + ".layer",
        ops = [merge_os_release({"IMAGE_ID": image_id, "IMAGE_VERSION": version})],
    )
    definitions = format_partition_labels(definitions, image_id, version)
    system_definitions = [definition for definition in definitions if definition.type != "esp"]
    boot_definitions = [definition for definition in definitions if definition.type == "esp"]
    if not system_definitions or not boot_definitions:
        fail("bootable_disk_image: definitions must include system and ESP partitions")
    repart(
        name = name + ".partitions",
        image = ":" + name + ".identity.layer",
        definitions = system_definitions,
        disk = False,
        split = True,
        seed = disk_seed,
        private_key = verity_private_key,
        certificate = verity_certificate,
    )
    verity = [definition for definition in system_definitions if definition.verity == "data"]
    uki(
        name = name + ".uki",
        image = ":" + name + ".identity.layer",
        initrds = [":" + name + ".initrd"],
        cmdline = cmdline,
        profiles = profiles,
        arch = arch,
        image_id = image_id,
        version = version,
        root_hash = ":" + name + ".partitions" if verity else None,
        visibility = visibility,
    )
    esp_ops = [
        copy(
            source = ":" + name + ".uki",
            destination = "/boot/EFI/Linux",
        ),
        install_systemd_boot(),
    ]

    # Copy caller-provided artifacts onto the ESP. The ESP partition's `copy_files = ["/boot:/", "/efi:/"]`
    # carries them onto the disk, so each destination must live under one of those trees.
    for destination, source in esp_files.items():
        if not (destination.startswith("/boot/") or destination.startswith("/efi/")):
            fail("bootable_disk_image: esp_files destination must be under /boot or /efi, got {!r}".format(
                destination,
            ))
        esp_ops.append(copy(source = source, destination = destination))

    image_layer(
        name = name + ".esp.layer",
        parent = ":" + name + ".identity.layer",
        ops = esp_ops,
    )
    repart(
        name = name,
        image = ":" + name + ".esp.layer",
        definitions = boot_definitions,
        partitions = [":" + name + ".partitions"],
        split = True,
        seed = disk_seed,
        visibility = visibility,
    )
    image_directory(
        name = name + ".directory",
        image = ":" + name + ".esp.layer",
        visibility = visibility,
    )

    # Alternative disk encodings are always addressable siblings (e.g. `:name.qcow2`); Buck only re-encodes
    # the one that is actually requested, so exposing them all costs nothing on a default build.
    for format in DISK_FORMATS:
        disk_convert(
            name = "{}.{}".format(name, format),
            disk = ":" + name,
            format = format,
            visibility = visibility,
        )

    # The initrd resolves its own package closure, so keep its supply-chain outputs separate from
    # the root filesystem's. Every artifact is an explicit sibling terminal target.
    image_pkgdb(
        name = name + ".pkgdb",
        image = ":" + name + ".esp.layer",
        visibility = visibility,
    )
    image_pkgdb(
        name = name + ".initrd.pkgdb",
        image = initrd,
        visibility = visibility,
    )
    image_sbom(
        name = name + ".sbom",
        image = ":" + name + ".esp.layer",
        source_name = name,
        version = version,
        visibility = visibility,
    )

    # A distinct source name keeps the two documents apart, including their normalized ids.
    image_sbom(
        name = name + ".initrd.sbom",
        image = initrd,
        source_name = name + ".initrd",
        version = version,
        visibility = visibility,
    )
