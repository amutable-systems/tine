"""Install operations a target attaches to itself, for any image to apply."""

load(
    ":image.bzl",
    "IMAGE_OPERATION_ATTR",
    "ImageInstallInfo",
    "LayerOperationTree",  # @unused Used as a type.
    "expand_install_operations",
    "flatten_operations",
)

def _image_install_impl(ctx: AnalysisContext) -> list[Provider]:
    operations = expand_install_operations(ctx.attrs.ops)

    # A convenience view: building this target builds what it would copy into an image.
    copies = {}
    for operation in operations:
        if operation[0] == "copy":
            copies[operation[1].short_path] = operation[1]
    return [
        DefaultInfo(default_outputs = [copies[path] for path in sorted(copies)]),
        ImageInstallInfo(operations = operations),
    ]

_image_install = rule(
    impl = _image_install_impl,
    attrs = {
        "ops": attrs.list(IMAGE_OPERATION_ATTR, doc = "operations an image applies to install this target"),
    },
)

def image_install(name: str, ops: list[LayerOperationTree], **kwargs) -> None:
    """Attach install operations to a target, applied by `install_from()` in an image."""
    if not ops:
        fail("image_install {}: declare the operations that install it".format(name))
    _image_install(name = name, ops = flatten_operations(ops), **kwargs)
