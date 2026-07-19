"""Run the unit-test suite inside an engine's hermetic sandbox."""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")

def _engine_unittest_impl(ctx: AnalysisContext) -> list[Provider]:
    # Copied, not symlinked: the tests resolve __file__ to locate the tool under test, which
    # must not escape into the source checkout behind buck's back.
    tree = ctx.actions.copied_dir("tree", ctx.attrs.srcs)
    passed = ctx.actions.declare_output("passed")
    ctx.actions.run(
        cmd_args(
            chroot_run(engine = ctx.attrs.engine[EngineInfo]),
            "sh",
            "-ec",
            'python3 -B -m unittest discover -s "$0/tests" -t "$0" && : > "$1"',
            tree,
            passed.as_output(),
        ),
        category = "unittest",
    )
    return [DefaultInfo(default_output = passed)]

engine_unittest = rule(
    impl = _engine_unittest_impl,
    attrs = {
        "engine": attrs.dep(providers = [EngineInfo], doc = "engine whose hermetic sandbox runs the tests"),
        "srcs": attrs.dict(
            attrs.string(),
            attrs.source(),
            doc = "the test tree: tree-relative path -> source file",
        ),
    },
)
