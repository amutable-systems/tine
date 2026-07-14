"""Logical filesystem images built as ordered overlay deltas."""

load("//engine:rules.bzl", "EngineInfo", "chroot_run")
load("//package:install.bzl", "resolve_packages")
load("//package:manager.bzl", "PackageManagerInfo")
load("//package:system.bzl", "PackageSystemInfo")

ImageInfo = provider(
    doc = "A logical filesystem image represented by an ordered delta stack.",
    fields = {
        "engine": provider_field(Dependency),
        "layers": provider_field(list[Artifact]),
        # Ownership is deliberately not represented: image outputs use uid/gid 0.
        "tmpfiles": provider_field(list[str]),
    },
)

def _image_impl(ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(),
        ImageInfo(engine = ctx.attrs.engine, layers = [], tmpfiles = []),
    ]

_image = rule(
    impl = _image_impl,
    attrs = {
        "engine": attrs.dep(
            providers = [EngineInfo],
            doc = "the execution environment fixed for this image and all derived artifacts",
        ),
    },
)

def image(name: str, **kwargs) -> None:
    """Start an empty logical image and fix its engine."""
    if not name.endswith(".image"):
        fail("image name must end with '.image': {}".format(name))
    _image(
        name = name,
        **kwargs
    )

# JSON operations replayed by the layer driver.

def run(cmd: list[str], chroot: bool = True) -> str:
    """Run `cmd` in the image, or in the engine with the image at /buildroot."""
    return json.encode({"op": "run", "cmd": cmd, "chroot": chroot})

def install(packages: list[str]) -> str:
    """Install native packages using the layer's package manager."""
    if not packages:
        fail("install: packages must not be empty")
    return json.encode({"op": "install", "packages": sorted(packages)})

def mkdir(path: str, mode: str | None = None) -> str:
    op = {"op": "mkdir", "path": path}
    if mode != None:
        op["mode"] = mode
    return json.encode(op)

def symlink(target: str, path: str) -> str:
    return json.encode({"op": "symlink", "target": target, "path": path})

def remove(path: str) -> str:
    return json.encode({"op": "remove", "path": path})

def _install_specs(raw: str) -> list[str] | None:
    operation = json.decode(raw)
    if operation.get("op") != "install":
        return None
    packages = operation.get("packages")
    if type(packages) != "list" or not packages:
        fail("image install operation has invalid packages: {}".format(packages))
    for package in packages:
        if type(package) != "string":
            fail("image install operation has invalid packages: {}".format(packages))
    return packages

def _image_layer_impl(ctx: AnalysisContext) -> list[Provider]:
    if not ctx.attrs.ops and not ctx.attrs.tmpfiles:
        fail("image_layer: needs ops or tmpfiles")

    parent = ctx.attrs.parent[ImageInfo]
    install_specs = None
    for raw in ctx.attrs.ops:
        specs = _install_specs(raw)
        if specs == None:
            continue
        if install_specs != None:
            fail("image_layer: at most one install operation is allowed per layer")
        install_specs = specs

    if ctx.attrs.extra_packages and install_specs == None:
        fail("image_layer: extra_packages requires an install operation")

    if not ctx.attrs.ops:
        return [
            DefaultInfo(default_output = parent.layers[-1]) if parent.layers else DefaultInfo(),
            ImageInfo(
                engine = parent.engine,
                layers = parent.layers,
                tmpfiles = parent.tmpfiles + ctx.attrs.tmpfiles,
            ),
        ]

    closure = None
    installer = None
    extra_packages = [dep[DefaultInfo].default_outputs[0] for dep in ctx.attrs.extra_packages]
    if install_specs != None:
        if ctx.attrs.package_manager == None:
            fail("image_layer: package_manager is required by install operations")
        package_manager = ctx.attrs.package_manager[PackageManagerInfo]
        if package_manager.engine.label != parent.engine.label:
            fail(
                "image_layer: package manager {} uses engine {}, but image {} is fixed to {}".format(
                    ctx.attrs.package_manager.label,
                    package_manager.engine.label,
                    ctx.attrs.parent.label,
                    parent.engine.label,
                ),
            )
        closure = resolve_packages(
            ctx,
            ctx.attrs.package_manager,
            install_specs,
            parent.layers,
            extra_packages,
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
    for op in ctx.attrs.ops:
        cmd.add("--op", op)
    ctx.actions.run(cmd, category = "image_layer")

    layers = parent.layers + [delta]

    return [
        DefaultInfo(default_output = delta),
        ImageInfo(
            engine = parent.engine,
            layers = layers,
            tmpfiles = parent.tmpfiles + ctx.attrs.tmpfiles,
        ),
    ]

image_layer = rule(
    impl = _image_layer_impl,
    attrs = {
        "package_manager": attrs.option(
            attrs.dep(providers = [PackageManagerInfo]),
            default = None,
            doc = "the configured native package manager used by install",
        ),
        "extra_packages": attrs.list(
            attrs.dep(),
            default = [],
            doc = "local package output directories overlaid at higher priority",
        ),
        "parent": attrs.dep(providers = [ImageInfo], doc = "the logical image to extend"),
        "ops": attrs.list(
            attrs.string(),
            default = [],
            doc = "ordered operation JSON (use install/run/mkdir/symlink/remove)",
        ),
        "tmpfiles": attrs.list(
            attrs.string(),
            default = [],
            doc = "deferred tmpfiles.d lines for paths, modes, and xattrs; ownership is ignored",
        ),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image:layer"),
    },
)

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
