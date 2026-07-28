"""Logical filesystem images built as ordered overlay deltas."""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
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
        # Accumulated install specs; they seed the local-packages closure of every derived layer
        # so lower-layer packages keep their local backing in later solves.
        "install_specs": provider_field(list[str], default = []),
    },
)

# buildifier: disable=name-conventions  (record type, conventionally UpperCamelCase)
LayerOperation = record(
    value = tuple,
)

# Recursive type aliases are unavailable, so nested lists become dynamic at this boundary.
# buildifier: disable=name-conventions  (type alias, conventionally UpperCamelCase)
LayerOperationTree = LayerOperation | list[typing.Any]

# Operations replayed by the layer driver.

# buildifier: disable=name-conventions  (record type, conventionally UpperCamelCase)
ArtifactRef = record(
    source = str,
)

def artifact(source: str) -> ArtifactRef:
    """Reference a declared artifact as a run() command argument.

    The argument is replaced with the materialized artifact's path. Only chroot = False
    commands can use this: build outputs are not visible inside the image.
    """
    return ArtifactRef(source = source)

def run(cmd: list[str | ArtifactRef], chroot: bool = True, env: dict[str, str] = {}) -> LayerOperation:
    """Run `cmd` in the image, or in the engine with the image at /buildroot.

    Command arguments are strings, or artifact() references to declared artifacts.
    """
    arguments = []
    for argument in cmd:
        if type(argument) == "string":
            arguments.append(argument)
        else:
            if chroot:
                fail("run: artifact() arguments require chroot = False")
            arguments.append(("input", argument.source))
    return LayerOperation(value = ("run", arguments, chroot, {name: env[name] for name in sorted(env)}))

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

def merge_os_release(fields: dict[str, str]) -> LayerOperation:
    """Merge quoted KEY="value" assignments into the image's /usr/lib/os-release."""
    return LayerOperation(value = ("os_release", fields))

def _install_specs(
        operation: tuple,
        package_sets: dict[str, list[str]] | None) -> list[str] | None:
    if operation[0] != "install":
        if operation[0] != "install_package_set":
            return None
        if package_sets == None:
            fail("image: install_package_set requires an image with a package manager")
        name = operation[1]
        packages = package_sets.get(name)
        if packages == None:
            fail("image: unknown package set {!r}".format(name))
    else:
        packages = operation[1]
    if not packages:
        fail("image install operation has invalid packages: {}".format(packages))
    return sorted(packages)

def _image_impl(ctx: AnalysisContext) -> list[Provider]:
    parent = ctx.attrs.parent
    if parent != None:
        if ctx.attrs.engine != None or ctx.attrs.package_manager != None:
            fail("image: parent cannot be combined with engine or package_manager")
        parent = parent[ImageInfo]
        engine = parent.engine
        package_manager = parent.package_manager
        layers = parent.layers
        tmpfiles = parent.tmpfiles
        parent_install_specs = parent.install_specs
    else:
        engine = ctx.attrs.engine
        package_manager = ctx.attrs.package_manager
        if package_manager != None:
            manager_engine = package_manager[PackageManagerInfo].engine
            if engine != None and engine.label != manager_engine.label:
                fail("image engine {} does not match package manager engine {}".format(
                    engine.label,
                    manager_engine.label,
                ))
            engine = manager_engine
        if engine == None:
            fail("image requires parent, package_manager, or engine")
        layers = []
        tmpfiles = []
        parent_install_specs = []

    package_sets = None
    if package_manager != None:
        package_sets = package_manager[PackageManagerInfo].package_sets
    install_specs = None
    operations = []
    for operation in ctx.attrs.ops:
        specs = _install_specs(operation, package_sets)
        if specs == None:
            operations.append(operation)
            continue
        if install_specs != None:
            fail("image: at most one install operation is allowed per layer")
        install_specs = specs
        operations.append(("install", specs))

    if not ctx.attrs.ops:
        tmpfiles = tmpfiles + ctx.attrs.tmpfiles
        return [
            DefaultInfo(default_output = layers[-1]) if layers else DefaultInfo(),
            ImageInfo(
                engine = engine,
                package_manager = package_manager,
                layers = layers,
                tmpfiles = tmpfiles,
                install_specs = parent_install_specs,
            ),
        ]

    closure = None
    installer = None
    if install_specs != None:
        if package_manager == None:
            fail("image: install requires an image with a package manager")
        package_manager_info = package_manager[PackageManagerInfo]
        closure = resolve_packages(
            ctx,
            package_manager,
            install_specs,
            layers,
            local_seed = parent_install_specs + install_specs,
        )
        installer = package_manager_info.package_system[PackageSystemInfo].install

    driver = chroot_run(engine = engine[EngineInfo], exe = ctx.attrs._driver)
    delta = ctx.actions.declare_output("delta", dir = True)
    cmd = cmd_args(driver, "--out", delta.as_output())
    if layers:
        work = ctx.actions.declare_output("overlay.work", dir = True)
        cmd.add("--work", work.as_output())
        for lower in layers:
            cmd.add("--lower", lower)
    if installer != None:
        info = installer[DefaultInfo]
        cmd.add(
            "--installer",
            cmd_args(info.default_outputs[0], hidden = info.other_outputs),
            "--packages-dir",
            closure,
        )
        for lang in ctx.attrs.install_langs:
            cmd.add("--install-langs", lang)
        if not ctx.attrs.install_docs:
            cmd.add("--no-docs")
    else:
        if ctx.attrs.install_langs:
            fail("image: install_langs requires an install operation")
        if not ctx.attrs.install_docs:
            fail("image: install_docs requires an install operation")
    manifest = ctx.actions.write_json(
        "operations.json",
        operations,
        with_inputs = True,
        has_content_based_path = False,
    )
    cmd.add("--operations", manifest)
    ctx.actions.run(cmd, category = "image")

    layers = layers + [delta]
    tmpfiles = tmpfiles + ctx.attrs.tmpfiles

    return [
        DefaultInfo(default_output = delta),
        ImageInfo(
            engine = engine,
            package_manager = package_manager,
            layers = layers,
            tmpfiles = tmpfiles,
            install_specs = parent_install_specs + (install_specs if install_specs != None else []),
        ),
    ]

_operation_attr = attrs.one_of(
    attrs.tuple(
        attrs.enum(["run"]),
        attrs.list(attrs.one_of(
            attrs.tuple(attrs.enum(["input"]), attrs.source(allow_directory = True)),
            attrs.string(),
        )),
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
    attrs.tuple(
        attrs.enum(["os_release"]),
        attrs.dict(attrs.string(), attrs.string()),
    ),
)

_image = rule(
    impl = _image_impl,
    attrs = {
        "engine": attrs.option(
            attrs.dep(providers = [EngineInfo]),
            default = None,
            doc = "the execution environment fixed for an initial image and all derived artifacts",
        ),
        "install_docs": attrs.bool(
            default = True,
            doc = "keep documentation; licenses are kept either way (default: keep it)",
        ),
        "install_langs": attrs.list(
            attrs.string(),
            default = [],
            doc = "keep translations only for these languages (default: keep all)",
        ),
        "ops": attrs.list(
            _operation_attr,
            default = [],
            doc = "ordered operations generated by the public helpers",
        ),
        "package_manager": attrs.option(
            attrs.dep(providers = [PackageManagerInfo]),
            default = None,
            doc = "the native package manager fixed for an initial image",
        ),
        "parent": attrs.option(
            attrs.dep(providers = [ImageInfo]),
            default = None,
            doc = "the logical image to extend instead of starting an initial image",
        ),
        "tmpfiles": attrs.list(
            attrs.string(),
            default = [],
            doc = "deferred tmpfiles.d lines for paths, modes, and xattrs; ownership is ignored",
        ),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image:layer"),
    },
)

def image(
        name: str,
        ops: list[LayerOperationTree] = [],
        **kwargs) -> None:
    """Create an initial image or apply one delta to a parent image."""
    _image(
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
    image(
        name = name,
        parent = parent,
        ops = [remove(path) for path in paths],
        visibility = visibility,
    )
