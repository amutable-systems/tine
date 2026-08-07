"""Logical filesystem images built as ordered overlay deltas."""

load("//:specs.bzl", "executable", "spec_args", "spec_argument")
load("//distribution:defs.bzl", "distribution_aliases", "distribution_attr")
load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load("//package:install.bzl", "resolve_packages")
load("//package:manager.bzl", "PackageManagerInfo")
load("//package:system.bzl", "PackageSystemInfo")
load(":sign.bzl", "SigningAccess", "SigningKeyInfo", "external_signing_execution", "key_source_arguments", "merge_signing_access")

ImageToolsInfo = provider(
    doc = "The pinned drivers every image rule and terminal output runs.",
    fields = {
        "archive": provider_field(Dependency),
        "artifacts": provider_field(Dependency),
        "boot": provider_field(Dependency),
        "convert": provider_field(Dependency),
        "disk": provider_field(Dependency),
        "layer": provider_field(Dependency),
        "sbom": provider_field(Dependency),
        "syft": provider_field(Dependency),
        "sysext": provider_field(Dependency),
        "uki": provider_field(Dependency),
    },
)

_TOOLS = [
    "archive",
    "artifacts",
    "boot",
    "convert",
    "disk",
    "layer",
    "sbom",
    "syft",
    "sysext",
    "uki",
]

def _image_tools_impl(ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(),
        ImageToolsInfo(**{name: getattr(ctx.attrs, name) for name in _TOOLS}),
    ]

image_tools = rule(
    impl = _image_tools_impl,
    attrs = {name: attrs.dep(providers = [RunInfo]) for name in _TOOLS},
)

# Every rule resolves its drivers through one bundle instead of a private attribute each.
IMAGE_TOOLS_ATTR = {
    "_tools": attrs.dep(providers = [ImageToolsInfo], default = "tine//image:tools"),
}

NAME_PATTERN = "^[a-zA-Z0-9._-]+$"

# "+" would collide with sd-boot's boot-counting suffixes and "~" is systemd's pre-release
# separator, so filename components accept the latter but never the former.
FILENAME_PATTERN = "^[a-zA-Z0-9._~-]+$"

# Versions additionally accept "^", systemd's post-release separator.
VERSION_PATTERN = "^[a-zA-Z0-9._~^-]+$"

def check_name(what: str, value: str, pattern: str = NAME_PATTERN) -> str:
    """Reject a name that cannot survive the path, label, or filename it ends up in."""
    if not regex_match(pattern, value):
        fail("invalid {}: {!r}".format(what, value))
    return value

def _path(identifier: str | None, name: str) -> str:
    return identifier + "/" + name if identifier != None else name

def declare_out(ctx: AnalysisContext, identifier: str | None, name: str, dir: bool = False) -> Artifact:
    """Declare an output, scoped to `identifier` when one composition declares several."""
    return ctx.actions.declare_output(_path(identifier, name), dir = dir)

def spec_path(identifier: str | None, driver: str) -> str:
    """Name a driver's spec, scoped like the outputs of the same composition step."""
    return _path(identifier, driver + ".spec.json")

ImageSbomInfo = provider(
    doc = "SPDX and CycloneDX SBOMs generated from one logical image.",
    fields = {
        "cyclonedx": provider_field(Artifact),
        "spdx": provider_field(Artifact),
    },
)

ImageInfo = provider(
    doc = "A logical filesystem image represented by an ordered delta stack and lazy metadata.",
    fields = {
        "engine": provider_field(Dependency),
        # Accumulated install specs; they seed the local-packages closure of every derived layer
        # so lower-layer packages keep their local backing in later solves.
        "install_specs": provider_field(list[str], default = []),
        "layers": provider_field(list[Artifact]),
        "package_manager": provider_field(Dependency | None, default = None),
        # Absent only when the image installs no packages and so has no package system.
        "pkgdb": provider_field(Artifact | None, default = None),
        "sbom": provider_field(ImageSbomInfo),
        # Ownership is deliberately not represented: image outputs use uid/gid 0.
        "tmpfiles": provider_field(list[str]),
    },
)

def _image_command(
    ctx: AnalysisContext,
    *,
    engine: Dependency,
    layers: list[Artifact],
    tmpfiles: list[str],
    exe: Dependency,
    driver: str,
    identifier: str | None,
    spec: dict[str, typing.Any],
    signing_access: SigningAccess | None = None,
) -> cmd_args:
    return cmd_args(
        chroot_run(
            engine = engine[EngineInfo],
            exe = exe,
            ro_binds = signing_access.ro_binds if signing_access else {},
            setenv = signing_access.setenv if signing_access else {},
        ),
        spec_args(
            ctx.actions,
            spec_path(identifier, driver),
            {"lower": layers, "tmpfiles": tmpfiles} | spec,
        ),
    )

def terminal_image_command(
    ctx: AnalysisContext,
    *,
    image: ImageInfo,
    exe: Dependency,
    driver: str,
    spec: dict[str, typing.Any],
    identifier: str | None = None,
    signing_access: SigningAccess | None = None,
) -> cmd_args:
    """Run a terminal driver against one finalized logical-image stack.

    The stack and its deferred tmpfiles join the driver's own fields in one spec. `signing_access`
    grants the driver what an externally held signing key needs from the host.
    """
    return _image_command(
        ctx,
        signing_access = signing_access,
        driver = driver,
        engine = image.engine,
        exe = exe,
        identifier = identifier,
        layers = image.layers,
        spec = spec,
        tmpfiles = image.tmpfiles,
    )

def image_metadata_subtargets(image: ImageInfo) -> dict[str, list[Provider]]:
    """Expose the canonical metadata carried by a logical image."""
    sub_targets = {
        "sbom": [
            DefaultInfo(
                default_outputs = [image.sbom.spdx, image.sbom.cyclonedx],
                sub_targets = {
                    "cyclonedx": [DefaultInfo(default_output = image.sbom.cyclonedx)],
                    "spdx": [DefaultInfo(default_output = image.sbom.spdx)],
                },
            ),
            image.sbom,
        ],
    }
    if image.pkgdb != None:
        sub_targets["pkgdb"] = [DefaultInfo(default_output = image.pkgdb)]
    return sub_targets

def image_providers(
    *,
    image: ImageInfo,
    default_outputs: list[Artifact] = [],
    other_outputs: list[Artifact] = [],
    sub_targets: dict[str, list[Provider]] = {},
    extra: list[Provider] = [],
) -> list[Provider]:
    """Publish a terminal result together with its logical image and lazy metadata views."""
    merged = dict(sub_targets)
    merged.update(image_metadata_subtargets(image))
    return [
        DefaultInfo(
            default_outputs = default_outputs,
            other_outputs = other_outputs,
            sub_targets = merged,
        ),
        image,
        image.sbom,
    ] + extra

ImageInstallInfo = provider(
    doc = "Packages and operations a target contributes to any image installing it.",
    fields = {
        "operations": provider_field(list[typing.Any]),
        "packages": provider_field(list[str], default = []),
    },
)

LayerOperation = tuple

# Recursive type aliases are unavailable, so nested lists become dynamic at this boundary.
LayerOperationTree = LayerOperation | list[typing.Any]

# Each architecture's EFI spelling (ukify's --efi-arch, which also names the boot stubs) and
# systemd spelling (systemd's %a specifier), which names boot artifacts. The uki driver reads both from
# its spec, so this table is the single source; extend it to enable more architectures.
ARCHES = {"x86_64": struct(efi = "x64", systemd = "x86-64")}

# Operations replayed by the layer driver.

def run(cmd: list[str | Artifact], env: dict[str, str] = {}, chroot: bool = False) -> LayerOperation:
    """Run `cmd` against the image, either with the engine's tooling or the image's own.

    By default the engine supplies the userspace and the image is mounted at /buildroot. With
    `chroot`, the command runs inside the image instead, and the project is bind-mounted at a
    fixed path which becomes the working directory, so an artifact argument resolves the same
    either way and a script the repository owns can simply be run.

    An argument names a build artifact by being one, or by spelling `$(location //target)` in a
    BUCK file. Buck's macro parser claims `$(...)`, so a shell substitution has to be written
    `\\$(...)`; an unescaped one fails to parse rather than reaching the shell.
    """
    return ("run", cmd, _environment(env), chroot)

def python(cmd: list[str | Artifact], env: dict[str, str] = {}, chroot: bool = False) -> LayerOperation:
    """Run a python script against the image, with the interpreter Buck already pins.

    `chroot` picks the image's view exactly as it does for `run`: mounted at /buildroot by default,
    or the script's own root. Either way the interpreter is the relocatable one Buck fetches for its
    own bootstrap, named through the project, so the image never needs a python of its own. `cmd`
    is the script and its arguments, each naming artifacts as a `run` argument does.
    """
    if not cmd:
        fail("python: cmd must not be empty")

    # -B: the script and its imports are project sources, which no build may write bytecode into.
    return run(["$(location tine//tools:python3)", "-B"] + cmd, env = env, chroot = chroot)

def _environment(env: dict[str, str]) -> dict[str, str]:
    return {name: env[name] for name in sorted(env)}

def mkdir(path: str, mode: str | None = None) -> LayerOperation:
    """Create a directory in the image, with `mode` when given."""
    return ("mkdir", path, mode)

def symlink(target: str, path: str) -> LayerOperation:
    """Create a symlink at `path` pointing at `target`."""
    return ("symlink", target, path)

def remove(path: str) -> LayerOperation:
    """Remove a path from the image."""
    return ("remove", path)

def copy(source: str | Artifact, destination: str) -> LayerOperation:
    """Copy a declared artifact to an absolute path in the image."""
    return ("copy", source, destination)

def install_from(target: str) -> LayerOperation:
    """Install what an `image_install()` target needs and apply the operations it attaches."""
    return ("install_from", target)

def expand_install_from(ops: list[typing.Any]) -> (list[str], list[typing.Any]):
    """Collect each install_from target's packages and splice its operations in place.

    One pass suffices at every level: a target's provider already carries expanded operations, and
    Starlark has neither recursion nor a while loop to do it any other way.
    """
    packages = []
    expanded = []
    for operation in ops:
        if operation[0] == "install_from":
            info = operation[1][ImageInstallInfo]
            packages += info.packages
            expanded += info.operations
        else:
            expanded.append(operation)
    return packages, expanded

def merge_os_release(fields: dict[str, str]) -> LayerOperation:
    """Merge quoted KEY="value" assignments into the image's /usr/lib/os-release."""
    return ("os_release", fields)

def depmod() -> LayerOperation:
    """Rebuild the module indexes for every kernel the image installs, with the image's depmod."""
    return ("depmod",)

def hwdb(usr: bool = True, strict: bool = True) -> LayerOperation:
    """Compile the image's hwdb.d into the binary database udev reads.

    It lands in /usr, where the image ships it and nothing writable shadows it; `usr = False`
    writes the /etc copy instead. `strict` refuses a source file the image cannot parse.
    """
    return ("hwdb", usr, strict)

def locale_gen() -> LayerOperation:
    """Generate the locales the image's /etc/locale.gen asks for, with its own locale-gen."""
    return ("locale_gen",)

def sign_systemd_boot(key: SigningKeyInfo, arch: str) -> list[LayerOperation]:
    """Return operations that sign the image's systemd-boot binary as a `.signed` sibling.

    Sign before anything seals /usr, e.g. a verity partition; design.md explains why the signed
    binary must live in the image's own /usr. The key material never enters the image.
    """
    binary = "/buildroot/usr/lib/systemd/boot/efi/systemd-boot{}.efi".format(ARCHES[arch].efi)
    return [
        # systemd-sbsign lives outside PATH in the engine.
        run(
            [
                "/usr/lib/systemd/systemd-sbsign",
                "sign",
                "--private-key",
                key.private_key,
                "--certificate",
                key.certificate,
            ]
            + key_source_arguments(key)
            + [
                "--output=" + binary + ".signed",
                binary,
            ],
        ),
    ]

def install_systemd_boot(key: SigningKeyInfo | None = None) -> list[LayerOperation]:
    """Return operations that install systemd-boot into the image ESP staging paths.

    With a signing key, bootctl prefers the `.signed` binaries (see sign_systemd_boot) and
    writes loader/keys/auto enrollment variables, which sd-boot enrolls on firmware in
    setup mode. The key material never enters the image.
    """
    enroll = []
    if key != None:
        enroll = [
            "--secure-boot-auto-enroll=yes",
            "--certificate",
            key.certificate,
            "--private-key",
            key.private_key,
        ] + key_source_arguments(key)
    return [
        mkdir("/efi"),
        run(
            [
                "bootctl",
                "install",
                "--root=/buildroot",
                "--install-source=image",
                "--all-architectures",
                "--no-variables",
            ]
            + enroll,
            env = {
                "SYSTEMD_ESP_PATH": "/efi",
                "SYSTEMD_XBOOTLDR_PATH": "/boot",
            },
        ),
        remove("/efi/loader/random-seed"),
    ]

def _encode_operation(operation: LayerOperation) -> LayerOperation:
    if operation[0] != "run":
        return operation

    # An engine argument is a plain string, an artifact, or a resolved $(location) macro.
    return (
        operation[0],
        [spec_argument(argument) for argument in operation[1]],
        operation[2],
        operation[3],
    )

def _install_specs(
    packages: list[str],
    package_sets: list[str],
    available: dict[str, list[str]] | None,
) -> list[str]:
    """Resolve a layer's requested packages and symbolic sets into one deduplicated request."""
    specs = list(packages)
    for name in package_sets:
        if available == None:
            fail("image: package_sets requires an image with a package manager")
        members = available.get(name)
        if members == None:
            fail("image: unknown package set {!r}".format(name))
        specs += members
    for spec in specs:
        if not spec:
            fail("image: package names must not be empty")
    return sorted({spec: None for spec in specs})

def _declare_pkgdb(
    ctx: AnalysisContext,
    *,
    engine: Dependency,
    layers: list[Artifact],
    tmpfiles: list[str],
    package_manager: Dependency,
    identifier: str | None,
) -> Artifact:
    system = package_manager[PackageManagerInfo].package_system[PackageSystemInfo]
    out = declare_out(ctx, identifier, "pkgdb", dir = True)
    cmd = _image_command(
        ctx,
        driver = "pkgdb",
        engine = engine,
        exe = system.pkgdb,
        identifier = identifier,
        layers = layers,
        spec = {"out": out.as_output()},
        tmpfiles = tmpfiles,
    )
    ctx.actions.run(cmd, category = "image_pkgdb", identifier = identifier or "pkgdb")
    return out

def _declare_sbom(
    ctx: AnalysisContext,
    *,
    engine: Dependency,
    layers: list[Artifact],
    tmpfiles: list[str],
    source_name: str,
    version: str,
    identifier: str | None,
) -> ImageSbomInfo:
    tools = ctx.attrs._tools[ImageToolsInfo]
    spdx = declare_out(ctx, identifier, "sbom.spdx.json")
    cdx = declare_out(ctx, identifier, "sbom.cdx.json")
    cmd = _image_command(
        ctx,
        driver = "sbom",
        engine = engine,
        exe = tools.sbom,
        identifier = identifier,
        layers = layers,
        spec = {
            "cdx": cdx.as_output(),
            "source_name": source_name,
            "source_version": version,
            "spdx": spdx.as_output(),
            "syft": executable(tools.syft),
        },
        tmpfiles = tmpfiles,
    )
    ctx.actions.run(cmd, category = "image_sbom", identifier = identifier or "sbom")

    # One syft run emits both formats; selecting either format still executes the shared action.
    return ImageSbomInfo(cyclonedx = cdx, spdx = spdx)

def declare_image(
    ctx: AnalysisContext,
    *,
    ops: list[LayerOperation],
    packages: list[str] = [],
    package_sets: list[str] = [],
    identifier: str | None = None,
    parent: ImageInfo | None = None,
    engine: Dependency | None = None,
    package_manager: Dependency | None = None,
    tmpfiles: list[str] = [],
    install_docs: bool = True,
    install_langs: list[str] = [],
    source_name: str | None = None,
    version: str = "0",
    keys: list[SigningKeyInfo | None] = [],
) -> ImageInfo:
    """Declare one logical image layer from resolved providers, packages, and operations.

    The layer installs `packages` and `package_sets` as one request before its operations run, so
    every operation sees the packages this layer adds. `keys` names the signing keys this layer's
    operations use, so that the action can reach one held outside the build.
    """
    tools = ctx.attrs._tools[ImageToolsInfo]
    if parent != None:
        if engine != None or package_manager != None:
            fail("image: parent cannot be combined with engine or package_manager")
        engine = parent.engine
        package_manager = parent.package_manager
        layers = parent.layers
        tmpfiles = parent.tmpfiles + tmpfiles
        parent_install_specs = parent.install_specs
    else:
        if package_manager != None:
            manager_engine = package_manager[PackageManagerInfo].engine
            if engine != None and engine.label != manager_engine.label:
                fail(
                    "image engine {} does not match package manager engine {}".format(
                        engine.label,
                        manager_engine.label,
                    )
                )
            engine = manager_engine
        if engine == None:
            fail("image requires parent, package_manager, or engine")
        layers = []
        parent_install_specs = []

    available_sets = None
    if package_manager != None:
        available_sets = package_manager[PackageManagerInfo].package_sets
    from_targets, operations = expand_install_from(ops)
    install_specs = _install_specs(packages + from_targets, package_sets, available_sets)

    if operations or install_specs:
        closure = None
        installer = None
        if install_specs:
            if package_manager == None:
                fail("image: installing packages requires an image with a package manager")
            package_manager_info = package_manager[PackageManagerInfo]
            closure = resolve_packages(
                ctx,
                package_manager,
                install_specs,
                layers,
                identifier = identifier,
                local_seed = parent_install_specs + install_specs,
            )
            installer = package_manager_info.package_system[PackageSystemInfo].install

        delta = declare_out(ctx, identifier, "delta", dir = True)
        work = declare_out(ctx, identifier, "overlay.work", dir = True) if layers else None
        spec = {
            "install": None,
            "lower": layers,
            "operations": [_encode_operation(operation) for operation in operations],
            "out": delta.as_output(),
            "work": work.as_output() if work != None else None,
        }
        if installer != None:
            spec["install"] = {
                "arch": engine[EngineInfo].arch,
                "docs": install_docs,
                "installer": executable(installer),
                "langs": install_langs,
                "packages_dir": closure,
            }
        signing_access = merge_signing_access(keys)
        cmd = cmd_args(
            chroot_run(
                engine = engine[EngineInfo],
                exe = tools.layer,
                ro_binds = signing_access.ro_binds,
                setenv = signing_access.setenv,
            ),
            spec_args(ctx.actions, spec_path(identifier, "layer"), spec),
        )
        ctx.actions.run(cmd, category = "image", identifier = identifier or "layer", **external_signing_execution(signing_access))

        layers = layers + [delta]
        install_specs = parent_install_specs + install_specs
    else:
        install_specs = parent_install_specs

    pkgdb = None
    if package_manager != None:
        pkgdb = _declare_pkgdb(
            ctx,
            engine = engine,
            identifier = identifier,
            layers = layers,
            package_manager = package_manager,
            tmpfiles = tmpfiles,
        )
    return ImageInfo(
        engine = engine,
        install_specs = install_specs,
        layers = layers,
        package_manager = package_manager,
        pkgdb = pkgdb,
        sbom = _declare_sbom(
            ctx,
            engine = engine,
            identifier = identifier,
            layers = layers,
            source_name = source_name or ctx.label.name,
            tmpfiles = tmpfiles,
            version = version,
        ),
        tmpfiles = tmpfiles,
    )

IMAGE_OPERATION_ATTR = attrs.one_of(
    # A command takes attrs.arg() whether or not it chroots, so an artifact argument reads the
    # same either way; a shell substitution is escaped as `\\$(...)` at the call site.
    attrs.tuple(
        attrs.enum(["run"]),
        attrs.list(attrs.arg()),
        attrs.dict(attrs.string(), attrs.string()),
        attrs.bool(),
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
        attrs.enum(["install_from"]),
        attrs.dep(providers = [ImageInstallInfo]),
    ),
    attrs.tuple(
        attrs.enum(["os_release"]),
        attrs.dict(attrs.string(), attrs.string()),
    ),
    attrs.tuple(attrs.enum(["depmod"])),
    attrs.tuple(
        attrs.enum(["hwdb"]),
        attrs.bool(),
        attrs.bool(),
    ),
    attrs.tuple(attrs.enum(["locale_gen"])),
)

# The layer attributes every rule that builds a logical image from operations shares.
IMAGE_ATTRS = {
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
        IMAGE_OPERATION_ATTR,
        default = [],
        doc = "ordered operations generated by the public helpers, applied after the install",
    ),
    "package_sets": attrs.list(
        attrs.string(),
        default = [],
        doc = "symbolic package sets the image's OS release names, installed with `packages`",
    ),
    "packages": attrs.list(
        attrs.string(),
        default = [],
        doc = "native packages to install before this layer's operations run",
    ),
    "tmpfiles": attrs.list(
        attrs.string(),
        default = [],
        doc = "deferred tmpfiles.d lines for paths, modes, and xattrs; ownership is ignored",
    ),
    "version": attrs.string(default = "0", doc = "SBOM source version"),
} | IMAGE_TOOLS_ATTR

def _image_impl(ctx: AnalysisContext) -> list[Provider]:
    image = declare_image(
        ctx,
        engine = ctx.attrs.engine,
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = ctx.attrs.ops,
        package_manager = ctx.attrs.package_manager,
        package_sets = ctx.attrs.package_sets,
        packages = ctx.attrs.packages,
        parent = ctx.attrs.parent[ImageInfo] if ctx.attrs.parent != None else None,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
    )
    return image_providers(
        default_outputs = image.layers[-1:],
        image = image,
    )

_image = rule(
    impl = _image_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS
    | {
        "engine": attrs.option(
            attrs.dep(providers = [EngineInfo]),
            default = None,
            doc = "the execution environment fixed for an initial image and all derived artifacts",
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
    },
)

def flatten_operations(ops: list[LayerOperationTree]) -> list[LayerOperation]:
    """Flatten nested operation groups so helpers can return ordered sequences."""
    flattened = []
    for operation in ops:
        if type(operation) == "list":
            flattened.extend(flatten_operations(operation))
        else:
            flattened.append(operation)
    return flattened

def image(name: str, ops: list[LayerOperationTree] = [], **kwargs) -> None:
    """Create an initial image or apply one delta to a parent image."""
    distribution_aliases(name, kwargs)
    _image(name = name, ops = flatten_operations(ops), **distribution_attr(kwargs))
