# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Install operations a target attaches to itself, for any image to apply."""

load(
    ":image.bzl",
    "IMAGE_OPERATION_ATTR",
    "ImageInstallInfo",
    "LayerOperationTree",  # @unused Used as a type.
    "expand_install_from",
    "flatten_operations",
)

def _image_install_impl(ctx: AnalysisContext) -> list[Provider]:
    from_targets, operations = expand_install_from(ctx.attrs.ops)

    # A convenience view: building this target builds what it would copy into an image.
    copies = {}
    for operation in operations:
        if operation[0] == "copy":
            copies[operation[1].short_path] = operation[1]
    return [
        DefaultInfo(default_outputs = [copies[path] for path in sorted(copies)]),
        ImageInstallInfo(
            operations = operations,
            packages = sorted({package: None for package in ctx.attrs.packages + from_targets}),
        ),
    ]

_image_install = rule(
    impl = _image_install_impl,
    attrs = {
        "ops": attrs.list(IMAGE_OPERATION_ATTR, default = [], doc = "operations an image applies to install this target"),
        "packages": attrs.list(attrs.string(), default = [], doc = "native packages this target needs installed"),
    },
)

def image_install(name: str, ops: list[LayerOperationTree] = [], packages: list[str] = [], **kwargs) -> None:
    """Attach packages and install operations to a target, applied by `install_from()` in an image.

    An image installing this target adds its packages to its own install request, so a reusable
    target declares what it needs rather than leaving it to every caller.
    """
    if not ops and not packages:
        fail("image_install {}: declare the packages or operations that install it".format(name))
    _image_install(name = name, ops = flatten_operations(ops), packages = packages, **kwargs)
