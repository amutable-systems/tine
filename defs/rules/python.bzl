"""Run a python tool inside an engine root: the `chroot_python_run` helper and the
`chroot_python_binary` rule that wraps it as a standalone runnable."""

load("@prelude//python_bootstrap:python_bootstrap.bzl", "PythonBootstrapSources")
load(":providers.bzl", "EngineInfo")

ASSEMBLY_SDE = 1739577600

def chroot_python_run(
        ctx: AnalysisContext,
        sandbox: Dependency,
        engine: Provider,
        main: Artifact,
        deps: list[Dependency] = [],
        network: bool = False,
        label: str | None = None) -> RunInfo:
    """Build a runnable that runs `main` inside `engine` (an EngineInfo) via `sandbox`.

    `sandbox` only provides the exec environment; a driver that needs a target root to
    install into or run against sets it up itself (see rootfs.py)."""
    tree = {main.basename: main}
    for dep in deps:
        for s in dep[PythonBootstrapSources].srcs:
            tree[s.short_path] = s
    runtree = ctx.actions.copied_dir("__{}__".format(label or main.basename), tree)

    run = cmd_args(sandbox[RunInfo], "--tools", engine.root, "--bind-cwd")
    run.add("--source-date-epoch", str(ASSEMBLY_SDE))
    if network:
        run.add("--network")
    run.add("--", "/usr/bin/python3", runtree.project(main.basename))
    return RunInfo(args = run)

def _chroot_python_binary_impl(ctx: AnalysisContext) -> list[Provider]:
    """A standalone runnable executing a python tool inside an engine root."""
    run = chroot_python_run(
        ctx,
        sandbox = ctx.attrs._sandbox,
        engine = ctx.attrs.engine[EngineInfo],
        main = ctx.attrs.main,
        deps = ctx.attrs.deps,
        network = ctx.attrs.network,
        label = ctx.attrs.name,
    )
    return [DefaultInfo(), run]

chroot_python_binary = rule(
    impl = _chroot_python_binary_impl,
    attrs = {
        "main": attrs.source(doc = "the entry-point python module"),
        "deps": attrs.list(attrs.dep(providers = [PythonBootstrapSources]), default = [], doc = "library modules"),
        "engine": attrs.dep(providers = [EngineInfo], doc = "the engine whose root supplies /usr (python3 + libs)"),
        "network": attrs.bool(default = False, doc = "grant network (default: unshared/hermetic)"),
        "_sandbox": attrs.exec_dep(default = "tine//distribution:sandbox", providers = [RunInfo]),
    },
)
