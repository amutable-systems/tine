"""Build a Rust project from its own source tree, offline and SBOM-visible."""

load("//:specs.bzl", "executable", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load(":lock.bzl", "crate_downloads", "git_sources")
load(":vendor.bzl", "VENDOR_ATTRS", "assemble_vendor")

_PRIVATE = "__tine"

# All machinery lives under one deliberately private output name. The public output namespace then
# belongs to binaries, including names such as `git` that internals must not claim.

def _cargo_build_impl(
    actions: AnalysisActions,
    auditable: cmd_args,
    binaries: dict[str, OutputArtifact],
    build: RunInfo,
    fetch: RunInfo,
    lock: ArtifactValue,
    name: str,
    src: Artifact,
    target: OutputArtifact,
    vendor: RunInfo,
) -> list[Provider]:
    """Declare everything the lock names, once it has been built and can be read."""
    workspace = lock.read_json()
    resolved = workspace["lock"]
    repositories = {}
    for commit, fields in git_sources(name, resolved).items():
        # The prelude's git_fetch tool, run directly rather than through its rule: that rule hands
        # out the work tree, and cargo resolves a replaced git source against the repository.
        git_dir = actions.declare_output(_PRIVATE + "/git", commit[:12] + ".git", dir = True)
        work_tree = actions.declare_output(_PRIVATE + "/git", commit[:12] + ".work-tree", dir = True)
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
                _PRIVATE + "/cargo-build.spec.json",
                {
                    "auditable": auditable,
                    "binaries": binaries,
                    "git": repositories,
                    "root": workspace["root"],
                    "src": src,
                    "target": target,
                    "vendor": assemble_vendor(actions, vendor, crate_downloads(name, resolved), _PRIVATE),
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
        "src": dynattrs.value(Artifact),
        "target": dynattrs.output(),
        "vendor": dynattrs.value(RunInfo),
    },
)

def _cargo_package_impl(ctx: AnalysisContext) -> list[Provider]:
    # Symlinked, not copied: the build driver copies the tree into its scratch space anyway, because
    # cargo needs it writable, and copying twice buys nothing.
    src = ctx.actions.symlinked_dir(_PRIVATE + "/src", {source.short_path: source for source in ctx.attrs.srcs})
    reserved = [name for name in ctx.attrs.binaries if name == _PRIVATE or name.startswith(_PRIVATE + "/")]
    if reserved:
        fail("cargo_package {}: binaries may not be named {}".format(ctx.label.name, reserved))
    outputs = {name: ctx.actions.declare_output(name) for name in ctx.attrs.binaries}

    # Cargo's own build directory. An action's outputs are the only place it may leave state behind,
    # and buck clears them before rerunning it unless told not to.
    target = ctx.actions.declare_output(_PRIVATE + "/target", dir = True)

    resolved = ctx.actions.declare_output(_PRIVATE + "/workspace.json")
    ctx.actions.run(
        cmd_args(
            ctx.attrs._lock[RunInfo],
            spec_args(
                ctx.actions,
                _PRIVATE + "/cargo-lock.spec.json",
                {
                    "name": ctx.label.name,
                    "out": resolved.as_output(),
                    "sources": {source.short_path: source for source in ctx.attrs.srcs},
                },
            ),
        ),
        category = "cargo_lock",
    )

    ctx.actions.dynamic_output_new(
        _cargo_build(
            auditable = executable(ctx.attrs._auditable),
            binaries = {name: out.as_output() for name, out in outputs.items()},
            build = box_run(box = ctx.attrs.box[BoxInfo], exe = ctx.attrs._build),
            fetch = ctx.attrs._fetch[RunInfo],
            lock = resolved,
            name = ctx.label.name,
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
        "box": attrs.dep(providers = [BoxInfo], doc = "box carrying the Rust toolchain"),
        "srcs": attrs.list(attrs.source(), doc = "the project's source tree, Cargo.lock included"),
        "_auditable": attrs.exec_dep(providers = [RunInfo], default = "tine//tools:cargo-auditable"),
        "_build": attrs.exec_dep(providers = [RunInfo], default = "tine//cargo:build"),
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
