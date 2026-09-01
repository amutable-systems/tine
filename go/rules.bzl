"""Build a Go project from its own source tree: one online, verified module fetch, then an offline build."""

load("//:specs.bzl", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//git:defs.bzl", "git")

_PRIVATE = "__tine"

# All machinery lives under one deliberately private output name. The public output namespace then
# belongs to binaries, including names such as `src` that internals must not claim.

def _source(sources: dict[str, Artifact], path: str) -> Artifact:
    """The artifact holding `path`, taken out of a directory source when it sits inside one.

    Only these two files reach the fetch, rather than the tree they came with, so editing sources
    never refetches. They are found in the sources themselves because the symlinked build tree
    cannot be projected through the symlink standing in for a directory source.
    """
    if path in sources:
        return sources[path]
    for short_path, source in sources.items():
        prefix = short_path + "/"
        if path.startswith(prefix):
            return source.project(path[len(prefix) :])
    fail("go_package: {} is in none of the sources".format(path))

def _go_build_impl(
    actions: AnalysisActions,
    binaries: dict[str, OutputArtifact],
    build: RunInfo,
    cgo: bool | None,
    cgo_cflags: list[str],
    fetch: RunInfo,
    gocache: OutputArtifact | None,
    incremental: bool,
    linker_flags: list[str],
    sources: dict[str, Artifact],
    src: Artifact,
    tags: list[str],
    workspace: ArtifactValue,
) -> list[Provider]:
    """Declare what the project's go.mod asks for, once it has been found and can be read."""
    module = workspace.read_json()

    # The one online step: go downloads what go.mod names and verifies it against go.sum. Its
    # inputs are only those two files, so editing sources never refetches; and the cache is kept
    # across reruns, so a dependency bump downloads only what is missing from it.
    module_cache = None
    if module["sum"] != None:
        module_cache = actions.declare_output(_PRIVATE + "/module-cache", dir = True)
        actions.run(
            cmd_args(
                fetch,
                spec_args(
                    actions,
                    _PRIVATE + "/go-fetch.spec.json",
                    {
                        "mod": _source(sources, module["mod"]),
                        "module_cache_dir": module_cache.as_output(),
                        "sum": _source(sources, module["sum"]),
                    },
                ),
            ),
            allow_cache_upload = not incremental,
            category = "go_fetch",
            local_only = True,
            no_outputs_cleanup = incremental,
        )

    actions.run(
        cmd_args(
            build,
            spec_args(
                actions,
                _PRIVATE + "/go-build.spec.json",
                {
                    "binaries": binaries,
                    "cgo": cgo,
                    "cgo_cflags": cgo_cflags,
                    "gocache": gocache,
                    "linker_flags": linker_flags,
                    "module_cache_dir": module_cache,
                    "root": module["root"],
                    "src": src,
                    "tags": tags,
                },
            ),
        ),
        allow_cache_upload = not incremental,
        category = "go_build",
        no_outputs_cleanup = incremental,
    )
    return []

_go_build = dynamic_actions(
    impl = _go_build_impl,
    attrs = {
        "binaries": dynattrs.dict(str, dynattrs.output()),
        "build": dynattrs.value(RunInfo),
        "cgo": dynattrs.value(bool | None),
        "cgo_cflags": dynattrs.value(list[str]),
        "fetch": dynattrs.value(RunInfo),
        "gocache": dynattrs.option(dynattrs.output()),
        "incremental": dynattrs.value(bool),
        "linker_flags": dynattrs.value(list[str]),
        "sources": dynattrs.dict(str, dynattrs.value(Artifact)),
        "src": dynattrs.value(Artifact),
        "tags": dynattrs.value(list[str]),
        "workspace": dynattrs.artifact_value(),
    },
)

def _go_package_impl(ctx: AnalysisContext) -> list[Provider]:
    sources = {source.short_path: source for source in ctx.attrs.srcs}

    # Symlinked, not copied: the build driver copies the tree into its scratch space anyway, and
    # copying twice buys nothing.
    src = ctx.actions.symlinked_dir(_PRIVATE + "/src", sources)
    reserved = [name for name in ctx.attrs.binaries if name == _PRIVATE or name.startswith(_PRIVATE + "/")]
    if reserved:
        fail("go_package {}: binaries may not be named {}".format(ctx.label.name, reserved))
    outputs = {name: ctx.actions.declare_output(name) for name in ctx.attrs.binaries}

    # go's own build cache. An action's outputs are the only place it may leave state behind, and buck
    # clears them before rerunning it unless told not to. A declared output is also uploaded to the
    # cache, only do that for incremental builds; otherwise build in scratch space.
    gocache = ctx.actions.declare_output(_PRIVATE + "/gocache", dir = True) if ctx.attrs.incremental else None

    workspace = ctx.actions.declare_output(_PRIVATE + "/workspace.json")
    ctx.actions.run(
        cmd_args(
            ctx.attrs._workspace[RunInfo],
            spec_args(
                ctx.actions,
                _PRIVATE + "/go-workspace.spec.json",
                {
                    "name": ctx.label.name,
                    "out": workspace.as_output(),
                    "sources": sources,
                },
            ),
        ),
        allow_cache_upload = True,
        category = "go_workspace",
    )

    ctx.actions.dynamic_output_new(
        _go_build(
            binaries = {name: out.as_output() for name, out in outputs.items()},
            build = box_run(box = ctx.attrs.box[BoxInfo], exe = ctx.attrs._build),
            cgo = ctx.attrs.cgo,
            cgo_cflags = ctx.attrs.cgo_cflags,
            fetch = box_run(box = ctx.attrs.box[BoxInfo], exe = ctx.attrs._fetch, network = True),
            gocache = gocache.as_output() if gocache != None else None,
            incremental = ctx.attrs.incremental,
            linker_flags = ctx.attrs.linker_flags,
            sources = sources,
            src = src,
            tags = ctx.attrs.tags,
            workspace = workspace,
        ),
    )
    sub_targets = {name: [DefaultInfo(default_output = out)] for name, out in outputs.items()}
    return [DefaultInfo(default_outputs = outputs.values(), sub_targets = sub_targets)]

_go_package = rule(
    impl = _go_package_impl,
    attrs = {
        "binaries": attrs.list(attrs.string(), doc = "binaries to take out of the build"),
        "box": attrs.dep(providers = [BoxInfo], doc = "box carrying the Go toolchain"),
        "cgo": attrs.option(attrs.bool(), default = None, doc = "force cgo on or off, box toolchain default when unset"),
        "cgo_cflags": attrs.list(attrs.string(), default = [], doc = "extra C compiler flags for a cgo build"),
        "incremental": attrs.bool(doc = "keep go's caches across rebuilds of a mounted checkout"),
        "linker_flags": attrs.list(attrs.string(), default = [], doc = "flags for the Go linker, passed as -ldflags"),
        "srcs": attrs.list(attrs.source(), doc = "the project's source tree, go.mod and go.sum included"),
        "tags": attrs.list(attrs.string(), default = [], doc = "build tags selecting the project's optional files"),
        "_build": attrs.exec_dep(providers = [RunInfo], default = "tine//go:build"),
        "_fetch": attrs.exec_dep(providers = [RunInfo], default = "tine//go:fetch"),
        "_workspace": attrs.exec_dep(providers = [RunInfo], default = "tine//go:workspace"),
    },
)

def go_package(name: str, binaries: list[str], srcs: list[str] | None = None, **kwargs) -> None:
    """Build a checked-out Go project against the modules its go.sum pins.

    The sources default to the checkout named after the target. A go.mod among them marks the module
    root, and is read once the sources have been built, so a project whose tree arrives from a fetch
    needs nothing committed here. Each binary carries the name go itself would install it under.
    """
    if not binaries:
        fail("go_package {}: declare the binaries to take out of the build".format(name))
    _go_package(name = name, binaries = binaries, incremental = git.is_mount(name), srcs = srcs if srcs != None else glob([name + "/**"]), **kwargs)
