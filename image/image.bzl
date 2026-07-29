"""Logical filesystem images built as ordered overlay deltas."""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load("//package:install.bzl", "resolve_packages")
load("//package:manager.bzl", "PackageManagerInfo")
load("//package:system.bzl", "PackageSystemInfo")

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

def check_name(what: str, value: str, pattern: str = NAME_PATTERN) -> str:
    """Reject a name that cannot survive the path, label, or filename it ends up in."""
    if not regex_match(pattern, value):
        fail("invalid {}: {!r}".format(what, value))
    return value

def _path(identifier: str | None, name: str) -> str:
    return identifier + "/" + name if identifier != None else name

def declare_out(
        ctx: AnalysisContext,
        identifier: str | None,
        name: str,
        dir: bool = False) -> Artifact:
    """Declare an output, scoped to `identifier` when one composition declares several."""
    return ctx.actions.declare_output(_path(identifier, name), dir = dir)

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
        "package_manager": provider_field(Dependency | None, default = None),
        "layers": provider_field(list[Artifact]),
        # Ownership is deliberately not represented: image outputs use uid/gid 0.
        "tmpfiles": provider_field(list[str]),
        # Accumulated install specs; they seed the local-packages closure of every derived layer
        # so lower-layer packages keep their local backing in later solves.
        "install_specs": provider_field(list[str], default = []),
        "sbom": provider_field(ImageSbomInfo),
        # Absent only when the image installs no packages and so has no package system.
        "pkgdb": provider_field(Artifact | None, default = None),
    },
)

def _image_command(
        engine: Dependency,
        layers: list[Artifact],
        tmpfiles: list[str],
        exe: Dependency) -> cmd_args:
    cmd = cmd_args(chroot_run(engine = engine[EngineInfo], exe = exe))
    for lower in layers:
        cmd.add("--lower", lower)
    for snippet in tmpfiles:
        cmd.add("--tmpfiles", snippet)
    return cmd

def terminal_image_command(image: ImageInfo, exe: Dependency) -> cmd_args:
    """Run a terminal driver against one finalized logical-image stack."""
    return _image_command(image.engine, image.layers, image.tmpfiles, exe)

# buildifier: disable=function-docstring-args
# buildifier: disable=function-docstring-return
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

# buildifier: disable=function-docstring-args
# buildifier: disable=function-docstring-return
def image_providers(
        *,
        image: ImageInfo,
        default_outputs: list[Artifact] = [],
        other_outputs: list[Artifact] = [],
        sub_targets: dict[str, list[Provider]] = {},
        extra: list[Provider] = []) -> list[Provider]:
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

# buildifier: disable=name-conventions  (type alias, conventionally UpperCamelCase)
LayerOperation = tuple

# Recursive type aliases are unavailable, so nested lists become dynamic at this boundary.
# buildifier: disable=name-conventions  (type alias, conventionally UpperCamelCase)
LayerOperationTree = LayerOperation | list[typing.Any]

# Each architecture's EFI spelling (ukify's --efi-arch, which also names the boot stubs) and
# systemd spelling (systemd's %a specifier), which names boot artifacts. The uki driver takes both
# on its command line, so this table is the single source; extend it to enable more architectures.
ARCHES = {"x86_64": struct(efi = "x64", systemd = "x86-64")}

# Operations replayed by the layer driver.

def run(cmd: list[str | Artifact], env: dict[str, str] = {}) -> LayerOperation:
    """Run `cmd` with the engine's own tooling and the image mounted at /buildroot.

    An argument names a build artifact by being one, or by spelling `$(location //target)`
    in a BUCK file.
    """
    return ("run", cmd, _environment(env))

def chroot(cmd: list[str], env: dict[str, str] = {}) -> LayerOperation:
    """Run `cmd` with the image's own binaries, chrooted into it.

    Build outputs are not visible inside the image, so use run() for anything that needs one.
    """
    for argument in cmd:
        if type(argument) != "string":
            fail("chroot: artifact arguments require run()")

        # A chrooted command is coerced as a plain string, so Buck would leave this macro
        # verbatim for the shell. Other `$(...)` text is a shell substitution and stays.
        if "$(location" in argument:
            fail("chroot: $(location) arguments require run()")
    return ("chroot", cmd, _environment(env))

def _environment(env: dict[str, str]) -> dict[str, str]:
    return {name: env[name] for name in sorted(env)}

def install(packages: list[str]) -> LayerOperation:
    """Install native packages using the layer's package manager."""
    if not packages:
        fail("install: packages must not be empty")
    return ("install", sorted(packages))

def install_package_set(name: str) -> LayerOperation:
    """Install a package set supplied by the image's OS release."""
    if not name:
        fail("install_package_set: name cannot be empty")
    return ("install_package_set", name)

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

def merge_os_release(fields: dict[str, str]) -> LayerOperation:
    """Merge quoted KEY="value" assignments into the image's /usr/lib/os-release."""
    return ("os_release", fields)

def sign_systemd_boot(
        private_key: Artifact,
        certificate: Artifact,
        arch: str) -> list[LayerOperation]:
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
                private_key,
                "--certificate",
                certificate,
                "--output=" + binary + ".signed",
                binary,
            ],
        ),
    ]

def install_systemd_boot(
        private_key: Artifact | None = None,
        certificate: Artifact | None = None) -> list[LayerOperation]:
    """Return operations that install systemd-boot into the image ESP staging paths.

    With signing credentials, bootctl prefers the `.signed` binaries (see sign_systemd_boot) and
    writes loader/keys/auto enrollment variables, which sd-boot enrolls on firmware in
    setup mode. The key material never enters the image.
    """
    if (private_key == None) != (certificate == None):
        fail("install_systemd_boot: private_key and certificate must be specified together")
    enroll = []
    if private_key != None:
        enroll = [
            "--secure-boot-auto-enroll=yes",
            "--certificate",
            certificate,
            "--private-key",
            private_key,
        ]
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
            ] + enroll,
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

    # An engine argument is a plain string, an artifact, or a resolved $(location) macro, and
    # write_json renders the last as a list unless it is concatenated into a single argument.
    arguments = [cmd_args(argument, delimiter = "") for argument in operation[1]]
    return (operation[0], arguments, operation[2])

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

def _declare_pkgdb(
        ctx: AnalysisContext,
        *,
        engine: Dependency,
        layers: list[Artifact],
        tmpfiles: list[str],
        package_manager: Dependency,
        identifier: str | None) -> Artifact:
    system = package_manager[PackageManagerInfo].package_system[PackageSystemInfo]
    out = declare_out(ctx, identifier, "pkgdb", dir = True)
    cmd = _image_command(engine, layers, tmpfiles, system.pkgdb)
    cmd.add("--out", out.as_output())
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
        identifier: str | None) -> ImageSbomInfo:
    tools = ctx.attrs._tools[ImageToolsInfo]
    spdx = declare_out(ctx, identifier, "sbom.spdx.json")
    cdx = declare_out(ctx, identifier, "sbom.cdx.json")
    cmd = _image_command(engine, layers, tmpfiles, tools.sbom)
    cmd.add("--syft", tools.syft[DefaultInfo].default_outputs[0])
    cmd.add("--source-name", source_name)
    cmd.add("--source-version", version)
    cmd.add("--spdx", spdx.as_output())
    cmd.add("--cdx", cdx.as_output())
    ctx.actions.run(cmd, category = "image_sbom", identifier = identifier or "sbom")

    # One syft run emits both formats; selecting either format still executes the shared action.
    return ImageSbomInfo(cyclonedx = cdx, spdx = spdx)

# buildifier: disable=function-docstring-args
# buildifier: disable=function-docstring-return
def declare_image(
        ctx: AnalysisContext,
        *,
        ops: list[LayerOperation],
        identifier: str | None = None,
        parent: ImageInfo | None = None,
        engine: Dependency | None = None,
        package_manager: Dependency | None = None,
        tmpfiles: list[str] = [],
        install_docs: bool = True,
        install_langs: list[str] = [],
        source_name: str | None = None,
        version: str = "0") -> ImageInfo:
    """Declare one logical image layer from resolved providers and operations."""
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
                fail("image engine {} does not match package manager engine {}".format(
                    engine.label,
                    manager_engine.label,
                ))
            engine = manager_engine
        if engine == None:
            fail("image requires parent, package_manager, or engine")
        layers = []
        parent_install_specs = []

    package_sets = None
    if package_manager != None:
        package_sets = package_manager[PackageManagerInfo].package_sets
    install_specs = None
    operations = []
    for operation in ops:
        specs = _install_specs(operation, package_sets)
        if specs == None:
            operations.append(operation)
            continue
        if install_specs != None:
            fail("image: at most one install operation is allowed per layer")
        install_specs = specs
        operations.append(("install", specs))

    if ops:
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
                identifier = identifier,
                local_seed = parent_install_specs + install_specs,
            )
            installer = package_manager_info.package_system[PackageSystemInfo].install

        command = chroot_run(engine = engine[EngineInfo], exe = tools.layer)
        delta = declare_out(ctx, identifier, "delta", dir = True)
        cmd = cmd_args(command, "--out", delta.as_output())
        if layers:
            work = declare_out(ctx, identifier, "overlay.work", dir = True)
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
            for lang in install_langs:
                cmd.add("--install-langs", lang)
            if not install_docs:
                cmd.add("--no-docs")
        manifest = ctx.actions.write_json(
            _path(identifier, "operations.json"),
            [_encode_operation(operation) for operation in operations],
            with_inputs = True,
            has_content_based_path = False,
        )
        cmd.add("--operations", manifest)
        ctx.actions.run(cmd, category = "image", identifier = identifier or "layer")

        layers = layers + [delta]
        install_specs = parent_install_specs + (install_specs if install_specs != None else [])
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
    # Only the engine command takes attrs.arg(), so a chrooted command is free to spell shell
    # substitutions as `$(...)` without escaping them away from Buck's macro parser.
    attrs.tuple(
        attrs.enum(["run"]),
        attrs.list(attrs.arg()),
        attrs.dict(attrs.string(), attrs.string()),
    ),
    attrs.tuple(
        attrs.enum(["chroot"]),
        attrs.list(attrs.string()),
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
        doc = "ordered operations generated by the public helpers",
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
    attrs = IMAGE_ATTRS | {
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

def image(
        name: str,
        ops: list[LayerOperationTree] = [],
        **kwargs) -> None:
    """Create an initial image or apply one delta to a parent image."""
    _image(
        name = name,
        ops = flatten_operations(ops),
        **kwargs
    )
