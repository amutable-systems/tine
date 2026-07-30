"""Build a Rust project from its own source tree, offline and SBOM-visible."""

load("//:specs.bzl", "executable", "spec_args")
load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load(":lock.bzl", "crate_downloads")
load(":vendor.bzl", "VENDOR_ATTRS", "assemble_vendor")

_LOCK = "Cargo.lock"
_MANIFEST = "Cargo.toml"

# Names the rule declares for itself, as outputs or as sub-targets (vendor.bzl's among them). A binary
# sharing one would either collide in the output namespace or, worse, be shadowed by the sub-target of
# the same name.
_RESERVED = ("crate", "crates", "src", "vendor")

def _named(srcs: list[Artifact], name: str) -> list[Artifact]:
    return [src for src in srcs if src.short_path.rsplit("/", 1)[-1] == name]

def _directory(src: Artifact, name: str) -> str:
    return src.short_path[: -len(name)].rstrip("/")

def _workspace(label: Label, srcs: list[Artifact]) -> (Artifact | None, str):
    """The project's lock if it has one, and the workspace directory.

    A project is checked out, not written by us, so it carries no build file to point at its own
    root; cargo's workspace begins where its lock and manifest are. The lock is optional because a
    project that resolves nothing has nothing to pin, but then only one manifest may claim to be
    the root.
    """
    locks = _named(srcs, _LOCK)
    if len(locks) > 1:
        fail(
            "cargo_package {}: srcs hold several {} files: {}".format(
                label.name,
                _LOCK,
                [lock.short_path for lock in locks],
            )
        )
    manifests = _named(srcs, _MANIFEST)
    if locks:
        root = _directory(locks[0], _LOCK)
        manifests = [m for m in manifests if _directory(m, _MANIFEST) == root]
    elif not manifests:
        fail(
            "cargo_package {}: srcs hold no {}; by default the checkout is expected in the {}/ directory, pass `srcs` when it lives elsewhere".format(
                label.name,
                _MANIFEST,
                label.name,
            )
        )
    elif len(manifests) == 1:
        root = _directory(manifests[0], _MANIFEST)
    else:
        fail(
            "cargo_package {}: srcs hold no {} and {} {} files; commit the lock".format(
                label.name,
                _LOCK,
                len(manifests),
                _MANIFEST,
            )
        )
    if not manifests:
        fail("cargo_package {}: srcs hold no {} beside the {}".format(label.name, _MANIFEST, _LOCK))
    return (locks[0] if locks else None), root

def _cargo_package_impl(ctx: AnalysisContext) -> list[Provider]:
    lock, root = _workspace(ctx.label, ctx.attrs.srcs)
    if lock != None and ctx.attrs.crates == None:
        fail(
            ("cargo_package {}: srcs carry {} but no `lock` was passed; load it and pass it:\n" + '    load("//...:{}?format=toml", {}_lock = "value")').format(
                ctx.label.name,
                lock.short_path,
                _LOCK,
                ctx.label.name.replace("-", "_"),
            ),
        )
    if lock == None and ctx.attrs.crates != None:
        fail("cargo_package {}: a `lock` was passed but srcs hold no {}".format(ctx.label.name, _LOCK))
    vendor = assemble_vendor(ctx, ctx.attrs.crates or [])

    # Symlinked, not copied: the build driver copies the tree into its scratch space anyway, because
    # cargo needs it writable, and copying twice buys nothing.
    src = ctx.actions.symlinked_dir("src", {source.short_path: source for source in ctx.attrs.srcs})
    reserved = [name for name in ctx.attrs.binaries if name in _RESERVED]
    if reserved:
        fail("cargo_package {}: binaries may not be named {}".format(ctx.label.name, reserved))
    outputs = {name: ctx.actions.declare_output(name) for name in ctx.attrs.binaries}
    ctx.actions.run(
        cmd_args(
            chroot_run(engine = ctx.attrs.engine[EngineInfo], exe = ctx.attrs._build),
            spec_args(
                ctx,
                "cargo-build.spec.json",
                {
                    "auditable": executable(ctx.attrs._auditable),
                    "binaries": {name: out.as_output() for name, out in outputs.items()},
                    "root": root,
                    "src": src,
                    "vendor": vendor,
                },
            ),
        ),
        category = "cargo_build",
    )
    sub_targets = {name: [DefaultInfo(default_output = out)] for name, out in outputs.items()}
    return [DefaultInfo(default_outputs = outputs.values(), sub_targets = sub_targets)]

_cargo_package = rule(
    impl = _cargo_package_impl,
    attrs = {
        "binaries": attrs.list(attrs.string(), doc = "binaries to take out of the build"),
        "crates": attrs.option(
            attrs.list(attrs.dict(attrs.string(), attrs.string())),
            default = None,
            doc = "one download per registry crate, derived from the loaded lock; None without one",
        ),
        "engine": attrs.dep(providers = [EngineInfo], doc = "engine carrying the Rust toolchain"),
        "srcs": attrs.list(attrs.source(), doc = "the project's source tree, Cargo.lock included"),
        "_auditable": attrs.dep(providers = [RunInfo], default = "tine//tools:cargo-auditable"),
        "_build": attrs.dep(providers = [RunInfo], default = "tine//cargo:build"),
    }
    | VENDOR_ATTRS,
)

def cargo_package(name: str, binaries: list[str], srcs: list[str] | None = None, lock: dict[str, typing.Any] | None = None, **kwargs) -> None:
    """Build a checked-out Rust project against the crates its Cargo.lock pins.

    The sources default to the checkout named after the target, minus whatever a cargo build run
    inside it left behind. A project that commits a Cargo.lock passes it as `lock`, loaded with the
    format override, since data loads otherwise dispatch on the file suffix:

        load("//images/myproject:Cargo.lock?format=toml", myproject_lock = "value")
    """
    if not binaries:
        fail("cargo_package {}: declare the binaries to take out of the build".format(name))
    _cargo_package(
        name = name,
        binaries = binaries,
        crates = crate_downloads(name, lock) if lock != None else None,
        srcs = srcs if srcs != None else glob([name + "/**"], exclude = [name + "/target/**"]),
        **kwargs,
    )
