"""Run a package's unit tests inside an engine's hermetic sandbox."""

load("@prelude//python_bootstrap:python_bootstrap.bzl", "PythonBootstrapSources")
load("//engine:runtime.bzl", "EngineInfo", "chroot_run")

def _engine_unittest_impl(ctx: AnalysisContext) -> list[Provider]:
    tree = {}
    for dep in ctx.attrs.deps:
        for src in dep[PythonBootstrapSources].srcs:
            tree[src.short_path] = src
    for src in ctx.attrs.srcs:
        tree[src.short_path] = src

    # Copied, not symlinked: a test reaches the module under test through its own __file__, which
    # must not escape into the source checkout behind buck's back.
    root = ctx.actions.copied_dir("tree", tree)
    passed = ctx.actions.declare_output("passed")
    ctx.actions.run(
        cmd_args(
            chroot_run(engine = ctx.attrs.engine[EngineInfo]),
            "sh",
            "-ec",
            'python3 -B -m unittest discover -s "$0" -t "$0" -p "*_test.py" && : > "$1"',
            root,
            passed.as_output(),
        ),
        category = "unittest",
    )
    return [DefaultInfo(default_output = passed)]

engine_unittest = rule(
    doc = """A cached build action running one package's `*_test.py` files in a flat tree.

    Same-package sources go in `srcs`; anything from another package comes in through `deps` on a
    `python_bootstrap_library`. Both land beside the tests, so a test loads what it exercises from
    `Path(__file__).parent` -- which also covers the drivers that are not importable modules, the
    `.bzl` files written in the Python subset and the extensionless `tools/importer`.
    """,
    impl = _engine_unittest_impl,
    attrs = {
        "deps": attrs.list(
            attrs.dep(providers = [PythonBootstrapSources]),
            default = [],
            doc = "sources from other packages",
        ),
        "engine": attrs.dep(providers = [EngineInfo], doc = "engine whose hermetic sandbox runs the tests"),
        "srcs": attrs.list(attrs.source(), doc = "the tests and the sources they exercise"),
    },
)
