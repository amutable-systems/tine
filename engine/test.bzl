"""Run a package's unit tests inside an engine's hermetic sandbox."""

load("@prelude//python_bootstrap:python_bootstrap.bzl", "PythonBootstrapSources")
load("//engine:runtime.bzl", "EngineInfo", "chroot_run")

def _engine_python_test_impl(ctx: AnalysisContext) -> list[Provider]:
    tree = {}
    for source in [src for dep in ctx.attrs.deps for src in dep[PythonBootstrapSources].srcs] + ctx.attrs.srcs:
        # The tree is flat, so two files sharing a name would silently shadow one another and the
        # suite would exercise whichever landed last.
        if tree.get(source.short_path, source) != source:
            fail(
                "{}: {} and {} are both named {} in the flat tree; rename one".format(
                    ctx.label,
                    tree[source.short_path],
                    source,
                    source.short_path,
                )
            )
        tree[source.short_path] = source

    # Copied, not symlinked: a test reaches the module under test through its own __file__, which
    # must not escape into the source checkout behind buck's back.
    root = ctx.actions.copied_dir("tree", tree)

    # -B keeps __pycache__ out of the tree; discover's top-level dir defaults to the start dir.
    command = cmd_args(
        chroot_run(engine = ctx.attrs.engine[EngineInfo]),
        "python3",
        "-B",
        "-m",
        "unittest",
        "discover",
        "-s",
        root,
        "-p",
        "*_test.py",
    )
    return [
        # The tree so a failure can be reproduced by hand, and RunInfo so `buck run` on a suite
        # works; every prelude test rule exposes the latter through inject_test_run_info.
        DefaultInfo(default_output = root),
        RunInfo(args = command),
        ExternalRunnerTestInfo(
            type = "custom",
            command = [command],
            labels = ctx.attrs.labels,
            # The sandbox chroots, unshares and mounts, so it cannot be shipped to a remote
            # executor. tine's own execution platform is local-only, but a consuming project's
            # need not be.
            default_executor = CommandExecutorConfig(local_enabled = True, remote_enabled = False),
        ),
    ]

engine_python_test = rule(
    doc = """One package's `*_test.py` files, run by `buck test` in a flat tree inside an engine.

    Same-package sources go in `srcs`; anything from another package comes in through `deps` on a
    `python_bootstrap_library`. Both land beside the tests, so a test loads what it exercises from
    `Path(__file__).parent` -- which also covers the drivers that are not importable modules, the
    `.bzl` files written in the Python subset and the extensionless `tools/importer`.
    """,
    impl = _engine_python_test_impl,
    attrs = {
        "deps": attrs.list(
            attrs.dep(providers = [PythonBootstrapSources]),
            default = [],
            doc = "sources from other packages",
        ),
        "engine": attrs.dep(providers = [EngineInfo], doc = "engine whose hermetic sandbox runs the tests"),
        "labels": attrs.list(attrs.string(), default = [], doc = "passed to the test runner, for `buck test` filtering"),
        "srcs": attrs.list(attrs.source(), doc = "the tests and the sources they exercise"),
    },
)
