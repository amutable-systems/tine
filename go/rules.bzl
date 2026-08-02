"""Build a Go project from its own source tree: one online, verified module fetch, then an offline build."""

load("//:specs.bzl", "spec_args")
load("//engine:runtime.bzl", "EngineInfo", "chroot_run")

_MODULE = "go.mod"
_SUM = "go.sum"

# Output names the rule declares for itself. A binary sharing one collides in the output namespace,
# which buck reports as a duplicate path in buck-out rather than as the declaration being wrong.
_RESERVED = ("gocache", "module-cache", "src")

def _named(srcs: list[Artifact], name: str) -> list[Artifact]:
    return [src for src in srcs if src.short_path.rsplit("/", 1)[-1] == name]

def _directory(src: Artifact, name: str) -> str:
    return src.short_path[: -len(name)].rstrip("/")

def _depth(directory: str) -> int:
    return len(directory.split("/")) if directory else 0

def _outermost(label: Label, modules: list[Artifact]) -> Artifact:
    """The go.mod of the project, out of every go.mod the checkout carries.

    A second one belongs to a module nested in the project -- a tools or testdata helper, common
    enough in Go repositories -- and go leaves those out of a `./...` build by itself, so the one
    containing all the others is the project's own.
    """
    outer = modules[0]
    for module in modules:
        if _depth(_directory(module, _MODULE)) < _depth(_directory(outer, _MODULE)):
            outer = module
    root = _directory(outer, _MODULE)
    inside = root + "/" if root else ""
    strays = [src.short_path for src in modules if src != outer and not _directory(src, _MODULE).startswith(inside)]
    if strays:
        fail(
            "go_package {}: {} is not nested in {}, so srcs hold no single project; narrow `srcs` to one module".format(
                label.name,
                strays,
                outer.short_path,
            )
        )
    return outer

def _workspace(label: Label, srcs: list[Artifact]) -> (Artifact, Artifact | None, str):
    """The project's go.mod, its go.sum if it has one, and the module root directory.

    A project is checked out, not written by us, so it carries no build file to point at its own
    root; the go.mod marks it. The go.sum is optional because a module that resolves nothing has
    nothing to pin.
    """
    if _named(srcs, "go.work"):
        fail("go_package {}: go workspaces are not supported; keep go.work out of srcs".format(label.name))
    modules = _named(srcs, _MODULE)
    if not modules:
        fail(
            "go_package {}: srcs hold no {}; by default the checkout is expected in the {}/ directory, pass `srcs` when it lives elsewhere".format(
                label.name,
                _MODULE,
                label.name,
            )
        )
    module = _outermost(label, modules)
    root = _directory(module, _MODULE)
    sums = [sum for sum in _named(srcs, _SUM) if _directory(sum, _SUM) == root]
    return module, sums[0] if sums else None, root

def _go_package_impl(ctx: AnalysisContext) -> list[Provider]:
    module, sum, root = _workspace(ctx.label, ctx.attrs.srcs)

    # Symlinked, not copied: the build driver copies the tree into its scratch space anyway, and
    # copying twice buys nothing.
    src = ctx.actions.symlinked_dir("src", {source.short_path: source for source in ctx.attrs.srcs})
    reserved = [name for name in ctx.attrs.binaries if name in _RESERVED]
    if reserved:
        fail("go_package {}: binaries may not be named {}".format(ctx.label.name, reserved))
    outputs = {name: ctx.actions.declare_output(name) for name in ctx.attrs.binaries}

    # The one online step: go downloads what go.mod names and verifies it against go.sum. Its
    # inputs are only those two files, so editing sources never refetches; and the cache is kept
    # across reruns, so a dependency bump downloads only what is missing from it.
    module_cache = None
    if sum != None:
        module_cache = ctx.actions.declare_output("module-cache", dir = True)
        ctx.actions.run(
            cmd_args(
                chroot_run(engine = ctx.attrs.engine[EngineInfo], exe = ctx.attrs._fetch, network = True),
                spec_args(
                    ctx,
                    "go-fetch.spec.json",
                    {
                        "mod": module,
                        "module_cache_dir": module_cache.as_output(),
                        "sum": sum,
                    },
                ),
            ),
            category = "go_fetch",
            local_only = True,
            no_outputs_cleanup = True,
        )

    # go's own build cache. An action's outputs are the only place it may leave state behind, and
    # buck clears them before rerunning it unless told not to.
    gocache = ctx.actions.declare_output("gocache", dir = True)
    ctx.actions.run(
        cmd_args(
            chroot_run(engine = ctx.attrs.engine[EngineInfo], exe = ctx.attrs._build),
            spec_args(
                ctx,
                "go-build.spec.json",
                {
                    "binaries": {name: out.as_output() for name, out in outputs.items()},
                    "cgo": ctx.attrs.cgo,
                    "cgo_cflags": ctx.attrs.cgo_cflags,
                    "gocache": gocache.as_output(),
                    "module_cache_dir": module_cache,
                    "root": root,
                    "src": src,
                    "tags": ctx.attrs.tags,
                },
            ),
        ),
        category = "go_build",
        no_outputs_cleanup = True,
    )
    sub_targets = {name: [DefaultInfo(default_output = out)] for name, out in outputs.items()}
    return [DefaultInfo(default_outputs = outputs.values(), sub_targets = sub_targets)]

_go_package = rule(
    impl = _go_package_impl,
    attrs = {
        "binaries": attrs.list(attrs.string(), doc = "binaries to take out of the build"),
        "cgo": attrs.option(attrs.bool(), default = None, doc = "force cgo on or off, engine toolchain default when unset"),
        "cgo_cflags": attrs.list(attrs.string(), default = [], doc = "extra C compiler flags for a cgo build"),
        "engine": attrs.dep(providers = [EngineInfo], doc = "engine carrying the Go toolchain"),
        "srcs": attrs.list(attrs.source(), doc = "the project's source tree, go.mod and go.sum included"),
        "tags": attrs.list(attrs.string(), default = [], doc = "build tags selecting the project's optional files"),
        "_build": attrs.dep(providers = [RunInfo], default = "tine//go:build"),
        "_fetch": attrs.dep(providers = [RunInfo], default = "tine//go:fetch"),
    },
)

def go_package(name: str, binaries: list[str], srcs: list[str] | None = None, **kwargs) -> None:
    """Build a checked-out Go project against the modules its go.sum pins.

    The sources default to the checkout named after the target. Each binary carries the name go
    itself would install it under.
    """
    if not binaries:
        fail("go_package {}: declare the binaries to take out of the build".format(name))
    _go_package(
        name = name,
        binaries = binaries,
        srcs = srcs if srcs != None else glob([name + "/**"]),
        **kwargs,
    )
