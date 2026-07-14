"""Convenience macros for common image compositions."""

load("//image_format:archive.bzl", "image_archive")
load("//image_format:disk.bzl", "image_disk")
load(":boot.bzl", "boot_layer")
load(":layer.bzl", "image", "image_layer", "install", "run", "symlink")
load(":uki.bzl", "uki")

def rootfs_archive(
        name: str,
        package_manager: str,
        engine: str,
        ops: list[str],
        tmpfiles: list[str] = [],
        format: str = "tar",
        visibility: list[str] | None = None) -> None:
    """Build a root filesystem from ordered operations and archive it."""
    image(
        name = name + ".image",
        engine = engine,
    )
    image_layer(
        name = name + ".layer",
        package_manager = package_manager,
        parent = ":" + name + ".image",
        ops = ops,
        tmpfiles = tmpfiles,
    )
    image_archive(
        name = name,
        image = ":" + name + ".layer",
        format = format,
        visibility = visibility,
    )

# buildifier: disable=function-docstring-args
def bootable_disk_image(
        name: str,
        package_manager: str,
        engine: str,
        ops: list[str],
        tmpfiles: list[str] = [],
        initrd_ops: list[str] | None = None,
        cmdline: str = "console=hvc0 rw selinux=0",
        entry: str = "linux",
        boot_files: dict[str, str] = {},
        arch: str = "x86_64",
        kernel_version: str | None = None,
        disk_definitions: dict[str, str] = {},
        disk_seed: str | None = None,
        visibility: list[str] | None = None) -> None:
    """Build a UKI-based, systemd-boot GPT disk image."""
    image(
        name = name + ".image",
        engine = engine,
    )
    if initrd_ops == None:
        initrd_ops = [
            install(["systemd", "systemd-udev", "kmod", "bash"]),
            symlink("/usr/lib/systemd/systemd", "/init"),
            run(["/usr/bin/bash", "-c", ": > /etc/initrd-release"]),
        ]
    image_layer(
        name = name + ".initrd.layer",
        package_manager = package_manager,
        parent = ":" + name + ".image",
        ops = initrd_ops,
    )
    image_archive(
        name = name + ".initrd",
        image = ":" + name + ".initrd.layer",
        format = "cpio",
    )
    image_layer(
        name = name + ".layer",
        package_manager = package_manager,
        parent = ":" + name + ".image",
        ops = ops,
        tmpfiles = tmpfiles,
    )
    uki(
        name = name + ".uki",
        image = ":" + name + ".layer",
        initrds = [":" + name + ".initrd"],
        cmdline = cmdline,
        arch = arch,
        kernel_version = kernel_version,
    )
    files = dict(boot_files)
    files["/boot/EFI/Linux/" + entry + ".efi"] = ":" + name + ".uki"
    boot_layer(
        name = name + ".boot",
        parent = ":" + name + ".layer",
        files = files,
        bootloader = "systemd-boot",
    )
    image_disk(
        name = name,
        image = ":" + name + ".boot",
        definitions = disk_definitions,
        seed = disk_seed,
        visibility = visibility,
    )
