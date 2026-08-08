"""The conventional initrd a bootable image boots, as a target of its own."""

load("//distribution:defs.bzl", "distribution_aliases", "distribution_attrs")
load(
    "//image_format:archive.bzl",
    "COMPRESSIONS",
    "ImageArchiveInfo",
    "declare_image_archive",
)
load("//package:manager.bzl", "PackageManagerInfo")
load(
    ":image.bzl",
    "IMAGE_ATTRS",
    "ImageInfo",
    "LayerOperationTree",  # @unused Used as a type.
    "declare_image",
    "flatten_operations",
    "generated",
    "image_providers",
    "remove",
    "symlink",
)

InitrdInfo = provider(
    doc = "A logical initrd image and its derived cpio archive.",
    fields = {
        "cpio": provider_field(ImageArchiveInfo),
        "image": provider_field(ImageInfo),
    },
)

# The release names what an initrd of its own family needs, so this stays symbolic.
_DEFAULT_PACKAGE_SETS = ["initrd"]

_DEFAULT_OPS = [
    symlink("/usr/lib/systemd/systemd", "/init"),
    symlink("/etc/os-release", "/etc/initrd-release"),
]

# Pruned once the generators have read what they need: udev reads its binary hardware database at run
# time, not the sources it was compiled from, while these databases serve humans and service-name
# resolution, neither of which happens in the initrd.
_PRUNED = [
    remove("/usr/lib/udev/hwdb.d"),
    remove("/usr/lib/systemd/catalog"),
    remove("/var/lib/systemd/catalog"),
    remove("/etc/services"),
]

def _initrd_image_impl(ctx: AnalysisContext) -> list[Provider]:
    image = declare_image(
        ctx,
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = generated(_DEFAULT_OPS + ctx.attrs.ops, _DEFAULT_PACKAGE_SETS) + _PRUNED,
        package_manager = ctx.attrs.package_manager,
        package_sets = _DEFAULT_PACKAGE_SETS + ctx.attrs.package_sets,
        packages = ctx.attrs.packages,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
    )
    cpio = declare_image_archive(
        ctx,
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
    distribution: str | None = None,
    visibility: list[str] | None = None,
    **kwargs,
) -> None:
    """Declare the conventional initrd, extended by `packages`, `package_sets`, and `ops`.

    The release's `initrd` package set and the operations every initrd needs come first; what this
    target declares is added to them rather than replacing them. `bootable_disk_image` declares one
    of these for itself when it is given no `initrd`.
    """
    distribution_aliases(name, distribution, visibility)
    _initrd_image(name = name, ops = flatten_operations(ops), **(distribution_attrs(distribution, visibility) | kwargs))
