"""Convenience macros for common image compositions."""

load("//image_format:archive.bzl", "image_archive", "image_directory")
load(
    "//image_format:disk.bzl",
    "Partition",  # @unused Used as a type.
    "repart",
)
load("//image_format:rpmdb.bzl", "image_rpmdb")
load("//image_format:sbom.bzl", "image_sbom")
load("//image_format:sysext.bzl", "image_sysext")
load(":boot.bzl", "bootable", "install_systemd_boot", "uki")
load(
    ":layer.bzl",
    "LayerOperationTree",  # @unused Used as a type.
    "copy",
    "image",
    "image_layer",
    "install_package_set",
    "symlink",
)
load(":result.bzl", "image_result")

def rootfs_archive(
        name: str,
        package_manager: str,
        ops: list[LayerOperationTree],
        tmpfiles: list[str] = [],
        format: str = "tar",
        rpmdb: bool = False,
        sbom: bool = False,
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
    )
    rpmdb_facet = None
    if rpmdb:
        image_rpmdb(name = name + ".rpmdb", image = ":" + name + ".layer")
        rpmdb_facet = ":" + name + ".rpmdb"
    sbom_facet = None
    if sbom:
        image_sbom(name = name + ".sbom", image = ":" + name + ".layer", source_name = name, version = version)
        sbom_facet = ":" + name + ".sbom"
    image_archive(
        name = name,
        image = ":" + name + ".layer",
        format = format,
        rpmdb = rpmdb_facet,
        sbom = sbom_facet,
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
        rpmdb: bool = False,
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
    )
    rpmdb_facet = None
    if rpmdb:
        image_rpmdb(name = name + ".rpmdb", image = ":" + name + ".layer")
        rpmdb_facet = ":" + name + ".rpmdb"
    image_sysext(
        name = name,
        image = ":" + name + ".layer",
        base = base,
        release = release,
        seed = seed,
        rpmdb = rpmdb_facet,
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
        ],
    )
    image_archive(
        name = name + ".initrd",
        image = ":" + name + ".initrd.layer",
        format = "cpio",
    )
    return ":" + name + ".initrd"

# buildifier: disable=function-docstring-args
def bootable_disk_image(
        name: str,
        package_manager: str,
        ops: list[LayerOperationTree],
        definitions: list[Partition],
        tmpfiles: list[str] = [],
        initrd: str | None = None,
        cmdline: list[str] = ["root=tmpfs", "mount.usr=dissect", "rw"],
        entry: str = "linux",
        arch: str = "x86_64",
        disk_seed: str | None = None,
        verity_private_key: str | None = None,
        verity_certificate: str | None = None,
        rpmdb: bool = False,
        sbom: bool = False,
        version: str = "0",
        visibility: list[str] | None = None) -> None:
    """Build a UKI-based, systemd-boot GPT disk image."""
    image(
        name = name + ".image",
        package_manager = package_manager,
    )
    if initrd == None:
        initrd = _default_initrd(name, ":" + name + ".image")
    image_layer(
        name = name + ".layer",
        parent = ":" + name + ".image",
        ops = ops,
        tmpfiles = tmpfiles,
    )
    system_definitions = [definition for definition in definitions if definition.type != "esp"]
    boot_definitions = [definition for definition in definitions if definition.type == "esp"]
    if not system_definitions or not boot_definitions:
        fail("bootable_disk_image: definitions must include system and ESP partitions")
    repart(
        name = name + ".partitions",
        image = ":" + name + ".layer",
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
        image = ":" + name + ".layer",
        initrds = [initrd],
        cmdline = cmdline,
        arch = arch,
        entry = entry,
        root_hash = ":" + name + ".partitions" if verity else None,
    )
    image_layer(
        name = name + ".esp.layer",
        parent = ":" + name + ".layer",
        ops = [
            copy(
                source = ":" + name + ".uki",
                destination = "/boot/EFI/Linux",
            ),
            install_systemd_boot(),
        ],
    )
    repart(
        name = name + ".disk",
        image = ":" + name + ".esp.layer",
        definitions = boot_definitions,
        partitions = [":" + name + ".partitions"],
        split = True,
        seed = disk_seed,
    )
    bootable(
        name = name + ".bootable",
        image = ":" + name + ".esp.layer",
    )
    image_directory(
        name = name + ".directory",
        image = ":" + name + ".esp.layer",
    )

    # Supply-chain facets scan the same layer image_result aggregates, so their source labels match.
    rpmdb_facet = None
    if rpmdb:
        image_rpmdb(
            name = name + ".rpmdb",
            image = ":" + name + ".esp.layer",
        )
        rpmdb_facet = ":" + name + ".rpmdb"
    sbom_facet = None
    if sbom:
        image_sbom(
            name = name + ".sbom",
            image = ":" + name + ".esp.layer",
            source_name = name,
            version = version,
        )
        sbom_facet = ":" + name + ".sbom"

    image_result(
        name = name,
        image = ":" + name + ".esp.layer",
        bootable = ":" + name + ".bootable",
        directory = ":" + name + ".directory",
        disk = ":" + name + ".disk",
        rpmdb = rpmdb_facet,
        sbom = sbom_facet,
        default_facet = "disk",
        visibility = visibility,
    )
