# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build a Go project from its own source tree: one online, verified module fetch, then an offline build."""

load("//:specs.bzl", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//project:defs.bzl", "project")

_PRIVATE = "__tine"

# All machinery lives under one deliberately private output name. The public output namespace then
# belongs to binaries, including names such as `src` that internals must not claim.

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
    packages: dict[str, str],
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
                        "mod": src.project(module["mod"]),
                        "module_cache_dir": module_cache.as_output(),
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
                    "binaries": binaries,
                    "cgo": cgo,
                    "cgo_cflags": cgo_cflags,
                    "gocache": gocache,
                    "linker_flags": linker_flags,
                    "module_cache_dir": module_cache,
                    "packages": packages,
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
        "packages": dynattrs.value(dict[str, str]),
        "src": dynattrs.value(Artifact),
        "tags": dynattrs.value(list[str]),
        "workspace": dynattrs.artifact_value(),
    },
)

def _go_package_impl(ctx: AnalysisContext) -> list[Provider]:
    src = ctx.attrs.src

    # Most projects hold one program. Its import path is only known once the sources are built, so
    # the binary takes the target's name; the driver refuses a module with more than one candidate.
    packages = ctx.attrs.packages or {ctx.label.name: "./..."}
    names = packages.keys()
    for name in names:
        if name == _PRIVATE or name.startswith(_PRIVATE + "/"):
            fail("go_package: reserved output name {}".format(name))
        if not name or name in [".", ".."] or "/" in name or "\\" in name:
            fail("go_package: invalid output name {}".format(repr(name)))
    outputs = {name: ctx.actions.declare_output(name) for name in names}

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
                    "src": src,
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
            packages = packages,
            src = src,
            tags = ctx.attrs.tags,
            workspace = workspace,
        ),
    )
    # Run on the host, as a developer would after `go build -o`. The box is a build environment and
    # carries no runtime packages, so it is no better a place for a dynamically linked binary.
    sub_targets = {name: [DefaultInfo(default_output = out), RunInfo(args = cmd_args(out))] for name, out in outputs.items()}
    providers = [DefaultInfo(default_outputs = outputs.values(), sub_targets = sub_targets)]
    if len(outputs) == 1:
        providers.append(RunInfo(args = cmd_args(outputs.values()[0])))
    return providers

_go_package = rule(
    impl = _go_package_impl,
    attrs = {
        "box": attrs.exec_dep(providers = [BoxInfo], doc = "box carrying the Go toolchain"),
        "cgo": attrs.option(attrs.bool(), default = None, doc = "force cgo on or off, box toolchain default when unset"),
        "cgo_cflags": attrs.list(attrs.string(), default = [], doc = "extra C compiler flags for a cgo build"),
        "incremental": attrs.bool(doc = "keep go's caches across dev-mode rebuilds"),
        "linker_flags": attrs.list(attrs.string(), default = [], doc = "flags for the Go linker, passed as -ldflags"),
        "packages": attrs.dict(attrs.string(), attrs.string(), default = {}, doc = "output name to main package; unset builds the module's only one"),
        "src": attrs.source(allow_directory = True, doc = "the project's source directory, go.mod and go.sum included"),
        "tags": attrs.list(attrs.string(), default = [], doc = "build tags selecting the project's optional files"),
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

    The source defaults to the checkout named after the target. Its go.mod marks the module
    root, and is read once the source has been built, so a project whose tree arrives from a fetch
    needs nothing committed here.
    """
    _go_package(
        name = name,
        incremental = project.is_dev(name, source = src, override = dev),
        src = src if src != None else name,
        **kwargs,
    )
