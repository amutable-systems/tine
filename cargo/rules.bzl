"""Build a Rust project from its own source tree, offline and SBOM-visible."""

load("//:specs.bzl", "executable", "spec_args")
load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load(":lock.bzl", "crate_downloads", "git_sources")
load(":vendor.bzl", "VENDOR_ATTRS", "assemble_vendor")

_LOCK = "Cargo.lock"
_MANIFEST = "Cargo.toml"
_RESOLVED = "lock.json"

# Output names the rule declares for itself, vendor.bzl's among them. A binary sharing one collides in
# the output namespace, which buck reports as a duplicate path in buck-out rather than as the
# declaration being wrong.
_RESERVED = ("crate", "crates", "git", _RESOLVED, "src", "target", "vendor")

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

def _cargo_build_impl(
    actions: AnalysisActions,
    auditable: cmd_args,
    binaries: dict[str, OutputArtifact],
    build: RunInfo,
    fetch: RunInfo,
    lock: ArtifactValue,
    name: str,
    root: str,
    src: Artifact,
    target: OutputArtifact,
    vendor: RunInfo,
) -> list[Provider]:
    """Declare everything the lock names, once it has been built and can be read."""
    resolved = lock.read_json()
    repositories = {}
    for commit, fields in git_sources(name, resolved).items():
        # The prelude's git_fetch tool, run directly rather than through its rule: that rule hands
        # out the work tree, and cargo resolves a replaced git source against the repository.
        git_dir = actions.declare_output("git", commit[:12] + ".git", dir = True)
        work_tree = actions.declare_output("git", commit[:12] + ".work-tree", dir = True)
        actions.run(
            cmd_args(
                fetch,
                cmd_args(git_dir.as_output(), format = "--git-dir={}"),
                cmd_args(work_tree.as_output(), format = "--work-tree={}"),
                cmd_args(fields["git"], format = "--repo={}"),
                cmd_args(commit, format = "--rev={}"),
            ),
            category = "git_fetch",
            identifier = commit[:12],
            local_only = True,
        )
        repositories[commit] = {"fields": fields, "repo": git_dir}

    actions.run(
        cmd_args(
            build,
            spec_args(
                actions,
                "cargo-build.spec.json",
                {
                    "auditable": auditable,
                    "binaries": binaries,
                    "git": repositories,
                    "root": root,
                    "src": src,
                    "target": target,
                    "vendor": assemble_vendor(actions, vendor, crate_downloads(name, resolved)),
                },
            ),
        ),
        category = "cargo_build",
        no_outputs_cleanup = True,
    )
    return []

_cargo_build = dynamic_actions(
    impl = _cargo_build_impl,
    attrs = {
        "auditable": dynattrs.value(cmd_args),
        "binaries": dynattrs.dict(str, dynattrs.output()),
        "build": dynattrs.value(RunInfo),
        "fetch": dynattrs.value(RunInfo),
        "lock": dynattrs.artifact_value(),
        "name": dynattrs.value(str),
        "root": dynattrs.value(str),
        "src": dynattrs.value(Artifact),
        "target": dynattrs.output(),
        "vendor": dynattrs.value(RunInfo),
    },
)

def _cargo_package_impl(ctx: AnalysisContext) -> list[Provider]:
    lock, root = _workspace(ctx.label, ctx.attrs.srcs)

    # Symlinked, not copied: the build driver copies the tree into its scratch space anyway, because
    # cargo needs it writable, and copying twice buys nothing.
    src = ctx.actions.symlinked_dir("src", {source.short_path: source for source in ctx.attrs.srcs})
    reserved = [name for name in ctx.attrs.binaries if name in _RESERVED]
    if reserved:
        fail("cargo_package {}: binaries may not be named {}".format(ctx.label.name, reserved))
    outputs = {name: ctx.actions.declare_output(name) for name in ctx.attrs.binaries}

    # Cargo's own build directory. An action's outputs are the only place it may leave state behind,
    # and buck clears them before rerunning it unless told not to.
    target = ctx.actions.declare_output("target", dir = True)

    if lock != None:
        resolved = ctx.actions.declare_output(_RESOLVED)
        ctx.actions.run(
            cmd_args(ctx.attrs._lock[RunInfo], lock, resolved.as_output()),
            category = "cargo_lock",
        )
    else:
        # A project that resolves nothing takes the same path, with nothing to fetch.
        resolved = ctx.actions.write_json(_RESOLVED, {"package": []})

    ctx.actions.dynamic_output_new(
        _cargo_build(
            auditable = executable(ctx.attrs._auditable),
            binaries = {name: out.as_output() for name, out in outputs.items()},
            build = chroot_run(engine = ctx.attrs.engine[EngineInfo], exe = ctx.attrs._build),
            fetch = ctx.attrs._fetch[RunInfo],
            lock = resolved,
            name = ctx.label.name,
            root = root,
            src = src,
            target = target.as_output(),
            vendor = ctx.attrs._vendor[RunInfo],
        ),
    )
    sub_targets = {name: [DefaultInfo(default_output = out)] for name, out in outputs.items()}
    return [DefaultInfo(default_outputs = outputs.values(), sub_targets = sub_targets)]

_cargo_package = rule(
    impl = _cargo_package_impl,
    attrs = {
        "binaries": attrs.list(attrs.string(), doc = "binaries to take out of the build"),
        "engine": attrs.dep(providers = [EngineInfo], doc = "engine carrying the Rust toolchain"),
        "srcs": attrs.list(attrs.source(), doc = "the project's source tree, Cargo.lock included"),
        "_auditable": attrs.dep(providers = [RunInfo], default = "tine//tools:cargo-auditable"),
        "_build": attrs.dep(providers = [RunInfo], default = "tine//cargo:build"),
        "_fetch": attrs.exec_dep(providers = [RunInfo], default = "prelude//git/tools:git_fetch"),
        "_lock": attrs.exec_dep(providers = [RunInfo], default = "tine//cargo:lock"),
    }
    | VENDOR_ATTRS,
)

def cargo_package(name: str, binaries: list[str], srcs: list[str] | None = None, **kwargs) -> None:
    """Build a checked-out Rust project against the crates its Cargo.lock pins.

    The sources default to the checkout named after the target, minus whatever a cargo build run
    inside it left behind. A lock among them pins every fetch the build needs, and is read once it
    has been built, so a project whose tree arrives from a fetch needs nothing committed here.
    """
    if not binaries:
        fail("cargo_package {}: declare the binaries to take out of the build".format(name))
    _cargo_package(
        name = name,
        binaries = binaries,
        srcs = srcs if srcs != None else glob([name + "/**"], exclude = [name + "/target/**"]),
        **kwargs,
    )
