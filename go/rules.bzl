# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build and test a Go project from its own source tree: one online, verified module fetch, then an offline build."""

load("//:specs.bzl", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//project:defs.bzl", "project")

_PRIVATE = "__tine"

GoPackageInfo = provider(
    doc = "The test binaries of a go_package, for a go_test to run.",
    fields = {
        "box": provider_field(BoxInfo),
        "src": provider_field(Artifact),
        # The test binaries and the manifest that build.py writes beside them. It is None when the
        # go_package sets no `test_packages`.
        "tests": provider_field(Artifact | None),
    },
)

def _go_build_impl(
    actions: AnalysisActions,
    bin: OutputArtifact,
    build: RunInfo,
    build_mode: str | None,
    cgo: bool | None,
    cgo_cflags: list[str],
    fetch: RunInfo,
    gocache: OutputArtifact | None,
    incremental: bool,
    linker_flags: list[str],
    packages: dict[str, str],
    src: Artifact,
    tags: list[str],
    test_packages: list[str] | None,
    tests: OutputArtifact | None,
    workspace: ArtifactValue,
) -> list[Provider]:
    """Declare what the project's go.mod asks for, once it has been found and can be read."""
    module = workspace.read_json()

    # The fetch is the one online step: go downloads the modules go.mod requires and verifies them
    # against go.sum. The fetch reads only go.mod, go.sum and the go.mod of each module that go.mod
    # replaces with a directory, so an edit to a Go source file does not rerun it. The module cache
    # is kept across reruns, so a dependency bump downloads only the modules missing from it.
    module_cache = None
    if module["sum"] != None:
        module_cache = project.kept_dir(actions, _PRIVATE + "/module-cache")
        actions.run(
            cmd_args(
                fetch,
                spec_args(
                    actions,
                    _PRIVATE + "/go-fetch.spec.json",
                    {
                        "mod": module["mod"],
                        "module_cache_dir": module_cache.as_output(),
                        "modules": {path: src.project(path) for path in module["modules"]},
                        "sum": src.project(module["sum"]),
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
                    "bin": bin,
                    "build_mode": build_mode,
                    "cgo": cgo,
                    "cgo_cflags": cgo_cflags,
                    "gocache": gocache,
                    "linker_flags": linker_flags,
                    "module_cache_dir": module_cache,
                    "packages": packages,
                    "root": module["root"],
                    "src": src,
                    "tags": tags,
                    "test_packages": test_packages,
                    "tests": tests,
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
        "bin": dynattrs.output(),
        "build": dynattrs.value(RunInfo),
        "build_mode": dynattrs.value(str | None),
        "cgo": dynattrs.value(bool | None),
        "cgo_cflags": dynattrs.value(list[str]),
        "fetch": dynattrs.value(RunInfo),
        "gocache": dynattrs.option(dynattrs.output()),
        "incremental": dynattrs.value(bool),
        "linker_flags": dynattrs.value(list[str]),
        "packages": dynattrs.value(dict[str, str]),
        "src": dynattrs.value(Artifact),
        "tags": dynattrs.value(list[str]),
        "test_packages": dynattrs.value(list[str] | None),
        "tests": dynattrs.option(dynattrs.output()),
        "workspace": dynattrs.artifact_value(),
    },
)

def _go_package_impl(ctx: AnalysisContext) -> list[Provider]:
    src = ctx.attrs.src

    # Most projects hold one program. Its import path is only known once the sources are built, so
    # the binary takes the target's name; the driver refuses a module with more than one candidate.
    packages = ctx.attrs.packages or {ctx.label.name: "./..."}
    bin, outputs = project.binaries_dir(ctx.actions, _PRIVATE + "/bin", packages.keys(), "go_package {}: packages".format(ctx.label.name))

    # go's own build cache. An action's outputs are the only place it may leave state behind, and buck
    # clears them before rerunning it unless told not to. A declared output is also uploaded to the
    # cache, only do that for incremental builds; otherwise build in scratch space.
    gocache = project.kept_dir(ctx.actions, _PRIVATE + "/gocache") if ctx.attrs.incremental else None
    tests = project.kept_dir(ctx.actions, _PRIVATE + "/tests") if ctx.attrs.test_packages != None else None

    # The go_workspace action checks that module_root contains a go.mod. go_workspace runs only after
    # src is built, and building src can mean a git fetch. Checking the form of the path at analysis
    # time reports an empty, absolute or `..` path before src is built.
    module_root = ctx.attrs.module_root
    if module_root != None and (not module_root or module_root.startswith("/") or ".." in module_root.split("/")):
        fail("go_package {}: module_root must be a relative path inside src without '..': {}".format(ctx.label.name, repr(module_root)))

    workspace = ctx.actions.declare_output(_PRIVATE + "/workspace.json")
    ctx.actions.run(
        cmd_args(
            ctx.attrs._workspace[RunInfo],
            spec_args(
                ctx.actions,
                _PRIVATE + "/go-workspace.spec.json",
                {
                    "module_root": module_root,
                    "name": ctx.label.name,
                    "out": workspace.as_output(),
                    "src": src,
                },
            ),
        ),
        allow_cache_upload = True,
        category = "go_workspace",
    )

    ctx.actions.dynamic_output_new(
        _go_build(
            bin = bin.as_output(),
            build = box_run(box = ctx.attrs.box[BoxInfo], exe = ctx.attrs._build),
            build_mode = ctx.attrs.build_mode,
            cgo = ctx.attrs.cgo,
            cgo_cflags = ctx.attrs.cgo_cflags,
            fetch = box_run(box = ctx.attrs.box[BoxInfo], exe = ctx.attrs._fetch, network = True),
            gocache = gocache.as_output() if gocache != None else None,
            incremental = ctx.attrs.incremental,
            linker_flags = ctx.attrs.linker_flags,
            packages = packages,
            src = src,
            tags = ctx.attrs.tags,
            test_packages = ctx.attrs.test_packages,
            tests = tests.as_output() if tests != None else None,
            workspace = workspace,
        ),
    )
    return project.binaries_providers(outputs) + [GoPackageInfo(box = ctx.attrs.box[BoxInfo], src = src, tests = tests)]

_go_package = rule(
    impl = _go_package_impl,
    attrs = {
        "box": attrs.exec_dep(providers = [BoxInfo], doc = "box carrying the Go toolchain"),
        "build_mode": attrs.option(
            attrs.enum(["exe", "pie"]), default = None, doc = "go's -buildmode for the binaries and tests, the toolchain default when unset"
        ),
        "cgo": attrs.option(attrs.bool(), default = None, doc = "force cgo on or off, box toolchain default when unset"),
        "cgo_cflags": attrs.list(attrs.string(), default = [], doc = "extra C compiler flags for a cgo build"),
        "incremental": attrs.bool(doc = "keep go's caches across dev-mode rebuilds"),
        "linker_flags": attrs.list(attrs.string(), default = [], doc = "flags for the Go linker, passed as -ldflags"),
        "module_root": attrs.option(attrs.string(), default = None, doc = "directory of the go.mod to build, relative to src; the outermost go.mod when unset"),
        "packages": attrs.dict(attrs.string(), attrs.string(), default = {}, doc = "output name to main package; unset builds the module's only one"),
        "src": attrs.source(allow_directory = True, doc = "the project's source directory, go.mod and go.sum included"),
        "tags": attrs.list(attrs.string(), default = [], doc = "build tags selecting the project's optional files"),
        "test_packages": attrs.option(attrs.list(attrs.string()), default = None, doc = "Go package patterns whose tests the build compiles for a go_test"),
        "_build": attrs.exec_dep(providers = [RunInfo], default = "tine//go:build"),
        "_fetch": attrs.exec_dep(providers = [RunInfo], default = "tine//go:fetch"),
        "_workspace": attrs.exec_dep(providers = [RunInfo], default = "tine//go:workspace"),
    },
)

def go_package(
    name: str,
    src: str | None = None,
    dev: bool | None = None,
    **kwargs,
) -> None:
    """Build a checked-out Go project against the modules its go.sum pins.

    The source defaults to the checkout named after the target. Its outermost go.mod marks the
    module root unless `module_root` names another. The go.mod is read once the source has been
    built, so a project whose tree arrives from a fetch needs nothing committed here.
    """
    _go_package(
        name = name,
        incremental = project.is_dev(name, source = src, override = dev),
        src = src if src != None else name,
        **kwargs,
    )

def _go_test_impl(ctx: AnalysisContext) -> list[Provider]:
    package = ctx.attrs.package[GoPackageInfo]
    if package.tests == None:
        fail("go_test {}: {} sets no `test_packages`, so its build compiles no tests".format(ctx.label.name, ctx.attrs.package.label))
    command = cmd_args(
        box_run(box = package.box, exe = ctx.attrs._runner),
        spec_args(
            ctx.actions,
            "go-test.spec.json",
            {
                "args": ctx.attrs.args,
                "src": package.src,
                "tests": package.tests,
            },
        ),
    )
    return [
        DefaultInfo(),
        RunInfo(args = command),
        ExternalRunnerTestInfo(
            type = "custom",
            command = [command],
            labels = ctx.attrs.labels,
            # The sandbox chroots, unshares and mounts, so it cannot run on a remote executor.
            default_executor = CommandExecutorConfig(local_enabled = True, remote_enabled = False),
        ),
    ]

go_test = rule(
    doc = """Run the tests that a go_package's build compiled, by `buck test` inside the package's box.""",
    impl = _go_test_impl,
    attrs = {
        "args": attrs.list(attrs.string(), default = [], doc = "arguments for each test binary, such as -test.short"),
        "labels": attrs.list(attrs.string(), default = [], doc = "passed to the test runner, for `buck test` filtering"),
        "package": attrs.dep(providers = [GoPackageInfo], doc = "the go_package whose tests to run"),
        "_runner": attrs.exec_dep(providers = [RunInfo], default = "tine//go:runner"),
    },
)
