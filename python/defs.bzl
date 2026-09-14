"""Python bootstrap targets with an automatically generated, isolated ty check."""

load("@prelude//:rules.bzl", "python_bootstrap_binary", "python_bootstrap_library")
load("@prelude//python_bootstrap:python_bootstrap.bzl", "PythonBootstrapSources")
load("//box:runtime.bzl", "BoxInfo", "box_run")

_TYPECHECK_LABEL = "python-typecheck"

def flat_tree(srcs: list[Artifact], deps: list[Dependency]) -> dict[str, Artifact]:
    """The flat namespace a bootstrap target imports in: its own sources over its deps' transitive ones."""
    tree = {}
    for source in [source for dep in deps for source in dep[PythonBootstrapSources].srcs] + srcs:
        # The tree is flat, so two files sharing a name would silently shadow one another.
        if tree.get(source.short_path, source) != source:
            fail("{} and {} are both named {} in the flat tree; rename one".format(tree[source.short_path], source, source.short_path))
        tree[source.short_path] = source
    return tree

def _ty_check_impl(ctx: AnalysisContext) -> list[Provider]:
    # ty walks `.py` only, so an extensionless command has to be named for it to be checked; a source
    # written in the Starlark subset carries its own suffix and is not Python at all.
    check = [source.short_path for source in ctx.attrs.srcs if source.extension in ("", ".py")]
    if not check:
        return [DefaultInfo(default_output = ctx.actions.write("checked", ""))]

    # A dep's sources are here to resolve imports, not to be checked: each is checked by its own
    # target, in the environment that target declares.
    # ty takes its settings from a `pyproject.toml` it discovers, where its --config-file wants a
    # bare ty.toml; the check runs in a tree of its own, so the project's has to be laid out in it.
    contents = flat_tree(ctx.attrs.srcs, ctx.attrs.deps)
    contents["pyproject.toml"] = ctx.attrs._config
    tree = ctx.actions.symlinked_dir("tree", contents)

    # ty reports through its exit status alone, so stamp the output buck tracks once it is happy.
    runner = ctx.actions.write(
        "run.sh",
        [
            "#!/bin/sh",
            "set -eu",
            'output="$1"',
            "shift",
            '"$@"',
            ': > "$output"',
        ],
        is_executable = True,
    )

    # A driver's third-party imports resolve inside the box it runs in, so check it once per declared
    # box. Always name an environment, since ty otherwise falls back to the host interpreter and a
    # driver's imports would resolve against whatever the developer happens to have installed.
    environments = {box.label.name: cmd_args(box[BoxInfo].root, format = "{}/usr") for box in ctx.attrs.boxes}
    outputs = []
    for name, python in (environments or {"stdlib": cmd_args(ctx.attrs._python[DefaultInfo].default_outputs[0])}).items():
        output = ctx.actions.declare_output("checked-" + name)
        command = cmd_args(
            runner,
            output.as_output(),
            ctx.attrs._ty[RunInfo],
            "check",
            "--project",
            tree,
            "--python",
            python,
        )
        command.add([tree.project(path) for path in check])
        ctx.actions.run(command, category = "ty", identifier = name)
        outputs.append(output)
    return [DefaultInfo(default_outputs = outputs)]

_ty_check = rule(
    impl = _ty_check_impl,
    attrs = {
        "boxes": attrs.list(attrs.exec_dep(providers = [BoxInfo]), default = []),
        "deps": attrs.list(attrs.dep(providers = [PythonBootstrapSources]), default = []),
        "labels": attrs.list(attrs.string(), default = []),
        "srcs": attrs.list(attrs.source()),
        "_config": attrs.source(default = "tine//:ty-config"),
        "_python": attrs.exec_dep(default = "tine//tools:python3"),
        "_ty": attrs.exec_dep(default = "tine//tools:ty", providers = [RunInfo]),
    },
)

def ty_check(
    name: str,
    srcs: list[str] | Select,
    deps: list[str] | Select | None = None,
    boxes: list[str] | Select | None = None,
) -> None:
    """Check `srcs` in a flat tree of their transitive runtime, in every box named.

    The boxes hang off the check rather than the driver, which would otherwise depend on a box its
    own drivers build.
    """
    _ty_check(name = name, boxes = boxes, deps = deps, labels = [_TYPECHECK_LABEL], srcs = srcs)

def tine_python_library(
    name: str,
    srcs: list[str] | Select,
    deps: list[str] | Select | None = None,
    boxes: list[str] | Select | None = None,
    typecheck: bool = True,
    **kwargs,
) -> None:
    """Declare a flat bootstrap library and the ty check over its own sources."""
    python_bootstrap_library(name = name, srcs = srcs, deps = deps, **kwargs)
    if typecheck:
        ty_check(name = name + "-ty", srcs = srcs, deps = deps, boxes = boxes)

def tine_python_binary(
    name: str,
    main: str | Select,
    deps: list[str] | Select | None = None,
    boxes: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Declare a flat bootstrap binary and the ty check over its entry point."""
    python_bootstrap_binary(name = name, main = main, deps = deps, **kwargs)
    ty_check(name = name + "-ty", srcs = [main], deps = deps, boxes = boxes)

def _box_python_binary_impl(ctx: AnalysisContext) -> list[Provider]:
    tree = ctx.actions.symlinked_dir("tree", flat_tree([ctx.attrs.main], ctx.attrs.deps))
    command = cmd_args(
        box_run(ctx.attrs.box[BoxInfo], relaxed = True),
        "python3",
        "-B",
        tree.project(ctx.attrs.main.short_path),
    )
    return [DefaultInfo(default_output = tree), RunInfo(args = command)]

_box_python_binary = rule(
    doc = """A flat Python tree run by a box's own interpreter, host-integrated.

    For a tool whose third-party imports come from the box's packages: the bootstrap interpreter a
    `tine_python_binary` runs under has the standard library and nothing else. Relaxed, so the tool
    sees the host's network, environment and files, which is what a long-running service on the
    developer's machine needs.
    """,
    impl = _box_python_binary_impl,
    attrs = {
        "box": attrs.exec_dep(providers = [BoxInfo], doc = "box whose interpreter and packages run it"),
        "deps": attrs.list(attrs.dep(providers = [PythonBootstrapSources]), default = []),
        "main": attrs.source(doc = "the entry point"),
    },
)

def box_python_binary(
    name: str,
    box: str,
    main: str,
    deps: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Declare a box-hosted Python command and the ty check over its entry point, in that box."""
    _box_python_binary(name = name, box = box, main = main, deps = deps, **kwargs)
    ty_check(name = name + "-ty", srcs = [main], deps = deps, boxes = [box])
