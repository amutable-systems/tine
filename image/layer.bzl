"""Logical filesystem images built as ordered overlay deltas."""

load("//engine:rules.bzl", "EngineInfo", "chroot_run")
load("//image_format:actions.bzl", "archive_action")
load("//package:install.bzl", "resolve_packages")
load("//package:manager.bzl", "PackageManagerInfo")
load("//package:system.bzl", "PackageSystemInfo")

ImageInfo = provider(
    doc = "A logical filesystem image represented by an ordered delta stack.",
    fields = {
        "engine": provider_field(Dependency),
        "package_manager": provider_field(Dependency | None, default = None),
        "layers": provider_field(list[Artifact]),
        # Ownership is deliberately not represented: image outputs use uid/gid 0.
        "tmpfiles": provider_field(list[str]),
    },
)

# buildifier: disable=name-conventions  (record type, conventionally UpperCamelCase)
LayerOperation = record(
    value = tuple,
)

# Recursive type aliases are unavailable, so nested lists become dynamic at this boundary.
# buildifier: disable=name-conventions  (type alias, conventionally UpperCamelCase)
LayerOperationTree = LayerOperation | list[typing.Any]

def _image_impl(ctx: AnalysisContext) -> list[Provider]:
    engine = ctx.attrs.engine
    package_manager = ctx.attrs.package_manager
    if package_manager != None:
        manager_engine = package_manager[PackageManagerInfo].engine
        if engine != None and engine.label != manager_engine.label:
            fail("image engine {} does not match package manager engine {}".format(engine.label, manager_engine.label))
        engine = manager_engine
    if engine == None:
        fail("image requires package_manager or engine")
    return [
        DefaultInfo(),
        ImageInfo(engine = engine, package_manager = package_manager, layers = [], tmpfiles = []),
    ]

_image = rule(
    impl = _image_impl,
    attrs = {
        "engine": attrs.option(
            attrs.dep(providers = [EngineInfo]),
            default = None,
            doc = "the execution environment fixed for this image and all derived artifacts",
        ),
        "package_manager": attrs.option(
            attrs.dep(providers = [PackageManagerInfo]),
            default = None,
            doc = "the native package manager fixed for this image",
        ),
    },
)

def image(name: str, **kwargs) -> None:
    """Start an empty image and fix its package manager or engine."""
    if not name.endswith(".image"):
        fail("image name must end with '.image': {}".format(name))
    _image(
        name = name,
        **kwargs
    )

# Operations replayed by the layer driver.

def run(cmd: list[str], chroot: bool = True, env: dict[str, str] = {}) -> LayerOperation:
    """Run `cmd` in the image, or in the engine with the image at /buildroot."""
    return LayerOperation(value = ("run", cmd, chroot, {name: env[name] for name in sorted(env)}))

def install(packages: list[str]) -> LayerOperation:
    """Install native packages using the layer's package manager."""
    if not packages:
        fail("install: packages must not be empty")
    return LayerOperation(value = ("install", sorted(packages)))

def install_package_set(name: str) -> LayerOperation:
    """Install a package set supplied by the image's OS release."""
    if not name:
        fail("install_package_set: name cannot be empty")
    return LayerOperation(value = ("install_package_set", name))

def mkdir(path: str, mode: str | None = None) -> LayerOperation:
    return LayerOperation(value = ("mkdir", path, mode))

def symlink(target: str, path: str) -> LayerOperation:
    return LayerOperation(value = ("symlink", target, path))

def remove(path: str) -> LayerOperation:
    return LayerOperation(value = ("remove", path))

def copy(source: str, destination: str) -> LayerOperation:
    """Copy a declared artifact to an absolute path in the image."""
    return LayerOperation(value = ("copy", source, destination))

def _install_specs(
        operation: tuple,
        package_sets: dict[str, list[str]] | None) -> list[str] | None:
    if operation[0] != "install":
        if operation[0] != "install_package_set":
            return None
        if package_sets == None:
            fail("image_layer: install_package_set requires an image with a package manager")
        name = operation[1]
        packages = package_sets.get(name)
        if packages == None:
            fail("image_layer: unknown package set {!r}".format(name))
    else:
        packages = operation[1]
    if not packages:
        fail("image install operation has invalid packages: {}".format(packages))
    return sorted(packages)

def _directory_sub_targets(
        ctx: AnalysisContext,
        engine: Dependency,
        layers: list[Artifact],
        tmpfiles: list[str]) -> dict[str, list[Provider]]:
    rootfs = ctx.actions.declare_output("image.rootfs", dir = True)
    archive_action(
        ctx,
        engine,
        layers,
        tmpfiles,
        ctx.attrs._archive_driver,
        "directory",
        rootfs.as_output(),
    )
    return {"directory": [DefaultInfo(default_output = rootfs)]}

def _image_layer_impl(ctx: AnalysisContext) -> list[Provider]:
    if not ctx.attrs.ops and not ctx.attrs.tmpfiles:
        fail("image_layer: needs ops or tmpfiles")

    parent = ctx.attrs.parent[ImageInfo]
    package_sets = None
    if parent.package_manager != None:
        package_sets = parent.package_manager[PackageManagerInfo].package_sets
    install_specs = None
    operations = []
    for operation in ctx.attrs.ops:
        specs = _install_specs(operation, package_sets)
        if specs == None:
            operations.append(operation)
            continue
        if install_specs != None:
            fail("image_layer: at most one install operation is allowed per layer")
        install_specs = specs
        operations.append(("install", specs))

    if not ctx.attrs.ops:
        tmpfiles = parent.tmpfiles + ctx.attrs.tmpfiles
        sub_targets = _directory_sub_targets(ctx, parent.engine, parent.layers, tmpfiles)
        return [
            DefaultInfo(default_output = parent.layers[-1], sub_targets = sub_targets) if parent.layers else DefaultInfo(sub_targets = sub_targets),
            ImageInfo(
                engine = parent.engine,
                package_manager = parent.package_manager,
                layers = parent.layers,
                tmpfiles = tmpfiles,
            ),
        ]

    closure = None
    installer = None
    if install_specs != None:
        if parent.package_manager == None:
            fail("image_layer: install requires an image with a package manager")
        package_manager = parent.package_manager[PackageManagerInfo]
        closure = resolve_packages(
            ctx,
            parent.package_manager,
            install_specs,
            parent.layers,
        )
        installer = package_manager.package_system[PackageSystemInfo].install

    driver = chroot_run(engine = parent.engine[EngineInfo], exe = ctx.attrs._driver)
    delta = ctx.actions.declare_output("delta", dir = True)
    cmd = cmd_args(driver, "--out", delta.as_output())
    if parent.layers:
        work = ctx.actions.declare_output("overlay.work", dir = True)
        cmd.add("--work", work.as_output())
        for lower in parent.layers:
            cmd.add("--lower", lower)
    if installer != None:
        info = installer[DefaultInfo]
        cmd.add(
            "--installer",
            cmd_args(info.default_outputs[0], hidden = info.other_outputs),
            "--packages-dir",
            closure,
        )
    manifest = ctx.actions.write_json(
        "operations.json",
        operations,
        with_inputs = True,
        has_content_based_path = False,
    )
    cmd.add("--operations", manifest)
    ctx.actions.run(cmd, category = "image_layer")

    layers = parent.layers + [delta]
    tmpfiles = parent.tmpfiles + ctx.attrs.tmpfiles

    return [
        DefaultInfo(
            default_output = delta,
            sub_targets = _directory_sub_targets(ctx, parent.engine, layers, tmpfiles),
        ),
        ImageInfo(
            engine = parent.engine,
            package_manager = parent.package_manager,
            layers = layers,
            tmpfiles = tmpfiles,
        ),
    ]

_operation_attr = attrs.one_of(
    attrs.tuple(
        attrs.enum(["run"]),
        attrs.list(attrs.string()),
        attrs.bool(),
        attrs.dict(attrs.string(), attrs.string()),
    ),
    attrs.tuple(
        attrs.enum(["install"]),
        attrs.list(attrs.string()),
    ),
    attrs.tuple(
        attrs.enum(["install_package_set"]),
        attrs.string(),
    ),
    attrs.tuple(
        attrs.enum(["mkdir"]),
        attrs.string(),
        attrs.option(attrs.string(), default = None),
    ),
    attrs.tuple(
        attrs.enum(["symlink"]),
        attrs.string(),
        attrs.string(),
    ),
    attrs.tuple(
        attrs.enum(["remove"]),
        attrs.string(),
    ),
    attrs.tuple(
        attrs.enum(["copy"]),
        attrs.source(allow_directory = True),
        attrs.string(),
    ),
)

_image_layer = rule(
    impl = _image_layer_impl,
    attrs = {
        "parent": attrs.dep(providers = [ImageInfo], doc = "the logical image to extend"),
        "ops": attrs.list(
            _operation_attr,
            default = [],
            doc = "ordered operations generated by the public helpers",
        ),
        "tmpfiles": attrs.list(
            attrs.string(),
            default = [],
            doc = "deferred tmpfiles.d lines for paths, modes, and xattrs; ownership is ignored",
        ),
        "_archive_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:archive"),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image:layer"),
    },
)

def image_layer(
        name: str,
        ops: list[LayerOperationTree] = [],
        **kwargs) -> None:
    """Apply ordered operations as one persisted image delta."""
    _image_layer(
        name = name,
        ops = [operation.value for operation in _flatten_operations(ops)],
        **kwargs
    )

def _flatten_operations(ops: list[LayerOperationTree]) -> list[LayerOperation]:
    flattened = []
    for operation in ops:
        if type(operation) == "list":
            flattened.extend(_flatten_operations(operation))
        else:
            flattened.append(operation)
    return flattened

def image_cleanup(
        name: str,
        parent: str,
        paths: list[str],
        visibility: list[str] | None = None) -> None:
    """Create an explicit layer that removes configured image paths."""
    image_layer(
        name = name,
        parent = parent,
        ops = [remove(path) for path in paths],
        visibility = visibility,
    )
