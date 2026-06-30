"""Distribution rules: the format-neutral machinery //distribution composes."""

load("@prelude//python_bootstrap:python_bootstrap.bzl", "PythonBootstrapSources")
load(":providers.bzl", "DistributionInfo", "DriverInfo", "EngineInfo", "PackageFormatInfo", "RepoInfo")
load(":python.bzl", "chroot_python_run")

def _engine_root_impl(ctx: AnalysisContext) -> list[Provider]:
    fmt = ctx.attrs.package_format[PackageFormatInfo]
    packages = ctx.attrs.packages[DefaultInfo].default_outputs[0]

    # Action A — chroot1: payload-extract the whole seed (no db, no scripts) with
    # the format's bootstrap ur-tool.
    chroot1 = ctx.actions.declare_output("chroot1", dir = True)
    ctx.actions.run(
        cmd_args(fmt.extract[RunInfo], chroot1.as_output(), packages),
        category = "extract",
    )

    # Action B — chroot2: a *proper* install of the whole seed into a fresh root
    # (db + scriptlets), run with chroot1's tools.
    chroot2 = ctx.actions.declare_output("chroot2", dir = True)
    ctx.actions.run(
        cmd_args(
            chroot_python_run(
                ctx,
                sandbox = ctx.attrs._sandbox,
                engine = EngineInfo(root = chroot1),
                main = fmt.install.main,
                deps = fmt.install.deps,
            ),
            "--packages-dir",
            packages,
            "--target",
            chroot2.as_output(),
            "--resolv-symlink",
        ),
        category = "engine_root",
    )
    return [DefaultInfo(default_output = chroot2), EngineInfo(root = chroot2)]

engine_root = rule(
    impl = _engine_root_impl,
    attrs = {
        "packages": attrs.dep(doc = "the engine's package closure as one tree (//distribution:engine.<e>.packages)"),
        "package_format": attrs.dep(providers = [PackageFormatInfo], doc = "the package format plugin (extract + install drivers)"),
        "_sandbox": attrs.exec_dep(default = "tine//distribution:sandbox", providers = [RunInfo]),
    },
)

def _driver_impl(ctx: AnalysisContext) -> list[Provider]:
    return [DefaultInfo(), DriverInfo(main = ctx.attrs.main, deps = ctx.attrs.deps)]

# A chroot-run driver: an entry module + its libs, bound to an engine on demand by a
# consumer (via chroot_python_run, at the fixed assembly epoch). Aggregated into a format
# plugin by `package_format`.
driver = rule(
    impl = _driver_impl,
    attrs = {
        "main": attrs.source(doc = "the entry-point python module"),
        "deps": attrs.list(attrs.dep(providers = [PythonBootstrapSources]), default = [], doc = "library modules it imports"),
    },
)

def _package_format_impl(ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(),
        PackageFormatInfo(
            extract = ctx.attrs.extract,
            install = ctx.attrs.install[DriverInfo],
            createrepo = ctx.attrs.createrepo[DriverInfo],
            plan = ctx.attrs.plan[DriverInfo],
            build = ctx.attrs.build[DriverInfo],
        ),
    ]

# A package format plugin: every format-specific driver, declared in that format's
# subdir (`//distribution/<format>:package_format`) so the orchestration layer
# selects them by `data["package_format"]` without naming a format.
package_format = rule(
    impl = _package_format_impl,
    attrs = {
        "extract": attrs.exec_dep(providers = [RunInfo], doc = "the payload extractor / bootstrap ur-tool (Action A)"),
        "install": attrs.dep(providers = [DriverInfo], doc = "the install driver (cmdline install a set of packages into a root)"),
        "createrepo": attrs.dep(providers = [DriverInfo], doc = "the createrepo driver (repodata writer)"),
        "plan": attrs.dep(providers = [DriverInfo], doc = "the plan driver (resolver → transaction)"),
        "build": attrs.dep(providers = [DriverInfo], doc = "the package-build driver"),
    },
)

def _distribution_impl(ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(),
        DistributionInfo(
            engine = ctx.attrs.engine[EngineInfo],
            package_format = ctx.attrs.package_format[PackageFormatInfo],
            buildroot_repositories = [r[RepoInfo] for r in ctx.attrs.buildroot_repositories],
            buildroot_base_packages = ctx.attrs.buildroot_base_packages,
        ),
    ]

distribution = rule(
    impl = _distribution_impl,
    attrs = {
        "engine": attrs.dep(providers = [EngineInfo], doc = "the engine whose root the drivers run in"),
        "package_format": attrs.dep(providers = [PackageFormatInfo], doc = "the package format plugin supplying the build drivers"),
        "buildroot_repositories": attrs.list(attrs.dep(providers = [RepoInfo]), doc = "repos holding this distribution's buildroot packages"),
        "buildroot_base_packages": attrs.list(attrs.string(), doc = "always-installed base package names"),
    },
)

def _repo_impl(ctx: AnalysisContext) -> list[Provider]:
    # Bind the format's createrepo driver to run in the engine root (the repodata
    # writer lives there); chroot_python_run runs it at the fixed assembly epoch so the
    # repodata is reproducible.
    fmt = ctx.attrs.package_format[PackageFormatInfo]
    createrepo = chroot_python_run(
        ctx,
        sandbox = ctx.attrs._sandbox,
        engine = ctx.attrs.engine[EngineInfo],
        main = fmt.createrepo.main,
        deps = fmt.createrepo.deps,
    )
    packages = [p[DefaultInfo].default_outputs[0] for p in ctx.attrs.packages]

    # Generate the repository (packages + repodata/) so libdnf5 can resolve against
    # it as a real local repo.
    repo_dir = ctx.actions.declare_output("repo", dir = True)
    cmd = cmd_args(createrepo, "--out", repo_dir.as_output())
    for p in packages:
        cmd.add("--package", p)
    ctx.actions.run(cmd, category = "createrepo")

    return [
        DefaultInfo(default_output = repo_dir),
        RepoInfo(id = ctx.attrs.id, dir = repo_dir, packages = packages),
    ]

# A package repository: the packages processed (in an engine root) into a
# real local repository addressed by `id`, also exposing the per-package artifacts
# for closure materialization.
repo = rule(
    impl = _repo_impl,
    attrs = {
        "id": attrs.string(doc = "the repo id"),
        "packages": attrs.list(attrs.dep(), doc = "the rpms this repo publishes (one dep each)"),
        "engine": attrs.dep(providers = [EngineInfo], doc = "the engine whose root the createrepo driver runs in"),
        "package_format": attrs.dep(providers = [PackageFormatInfo], doc = "the package format plugin supplying the createrepo driver"),
        "_sandbox": attrs.exec_dep(default = "tine//distribution:sandbox", providers = [RunInfo]),
    },
)

def _fill_closure(actions: AnalysisActions, tx: ArtifactValue, closure: OutputArtifact, candidates: dict[str, Artifact]) -> list[Provider]:
    names = tx.read_json()
    actions.copied_dir(closure, {n: candidates[n] for n in names})
    return []

_materialize = dynamic_actions(
    impl = _fill_closure,
    attrs = {
        "tx": dynattrs.artifact_value(),
        "closure": dynattrs.output(),
        "candidates": dynattrs.dict(str, dynattrs.value(Artifact)),
    },
)

def _materialize_closure(ctx: AnalysisContext, name: str, tx: Artifact, candidates: dict[str, Artifact]) -> Artifact:
    """Scope a dir to just the packages named in the transaction `tx`.

    Dynamic: the resolved set is only known once the plan has run, so we read `tx` at
    build time and copy in exactly those package artifacts. The output is byte-identical
    for an unchanged transaction even though `candidates` (every repo package) is an
    input — that's what makes the install cut off when an unrelated repo package changes.
    """
    closure = ctx.actions.declare_output(name + ".closure", dir = True)
    ctx.actions.dynamic_output_new(_materialize(
        tx = tx,
        closure = closure.as_output(),
        candidates = candidates,
    ))
    return closure

# The assembled buildroot tree, carried out of the anon target for its `root` promise.
_BuildrootInfo = provider(fields = {"root": provider_field(Artifact)})

def _buildroot_impl(ctx: AnalysisContext) -> list[Provider]:
    """Resolve `install` over the distribution's repos and install the closure into a
    fresh root. Format-neutral: it drives the distribution's own plan + install drivers
    (`sandbox` is the launcher).

    Three actions: (1) plan resolves the install set's runtime closure over the repos →
    a transaction (resolved package filenames); (2) materialize scopes a dir to exactly
    those packages (dynamic, so unrelated repo changes cut off); (3) install lays that
    closure into a fresh root.
    """
    distribution = ctx.attrs.distribution[DistributionInfo]
    repos = distribution.buildroot_repositories
    fmt = distribution.package_format
    engine = distribution.engine
    sandbox = ctx.attrs.sandbox

    # Action 1 — plan: resolve the closure over the repos' repodata → a transaction.
    # Re-runs on any repo change, but its output is stable unless *this* closure changed.
    tx = ctx.actions.declare_output("transaction.json")
    plan = cmd_args(
        chroot_python_run(ctx, sandbox = sandbox, engine = engine, main = fmt.plan.main, deps = fmt.plan.deps),
        "--out",
        tx.as_output(),
    )
    for repository in repos:
        plan.add("--repo", cmd_args(repository.dir, format = repository.id + "={}"))
    for cap in ctx.attrs.install:
        plan.add("--install", cap)
    ctx.actions.run(plan, category = "plan")

    # Action 2 — materialize the resolved closure (scoped to the transaction).
    candidates = {p.basename: p for repository in repos for p in repository.packages}
    closure = _materialize_closure(ctx, "buildroot", tx, candidates)

    # Action 3 — install that closure into a fresh root (it's already the exact set).
    # Cuts off when the closure is unchanged.
    root = ctx.actions.declare_output("root", dir = True)
    ctx.actions.run(
        cmd_args(
            chroot_python_run(
                ctx,
                sandbox = sandbox,
                engine = engine,
                main = fmt.install.main,
                deps = fmt.install.deps,
            ),
            "--packages-dir",
            closure,
            "--target",
            root.as_output(),
        ),
        category = "assemble",
    )
    return [DefaultInfo(default_output = root), _BuildrootInfo(root = root)]

# A buildroot is pure data — (distribution, install set) fully determine it — so it's an
# anon target: two consumers with the same distribution and install set share one
# analysis (and thus one build), not just an action-cache hit. `sandbox` is forwarded as
# a resolved (exec-configured) dep because anon rules can't declare exec_dep.
_buildroot = anon_rule(
    impl = _buildroot_impl,
    attrs = {
        "distribution": attrs.dep(providers = [DistributionInfo]),
        "install": attrs.list(attrs.string()),
        "sandbox": attrs.dep(providers = [RunInfo]),
    },
    artifact_promise_mappings = {
        "root": lambda p: p[_BuildrootInfo].root,
    },
)

def assemble_root(ctx: AnalysisContext, distribution: Dependency, sandbox: Dependency, install: list[str]) -> Artifact:
    """Assemble a buildroot via the shared `_buildroot` anon target, returning its tree.
    `distribution` is the dep (not its DistributionInfo) so the anon target keys on it;
    `install` is sorted so equal sets share regardless of the order the caller assembled
    base + BuildRequires in. The tree's short path is the anon output's own (`root`) — a
    shared target can't take a per-caller name; `assert_short_path` just pins it so the
    promise is usable before the anon target is analyzed.
    """
    root = ctx.actions.anon_target(_buildroot, {
        # A friendlier log label than the default `anon//:_buildroot@<hash>`. Derived
        # purely from the distribution (already a key attr), so it doesn't split sharing;
        # the `@<hash>` buck appends still distinguishes distinct install sets.
        "name": "//buildroot:{}".format(distribution.label.name),
        "distribution": distribution,
        "install": sorted(install),
        "sandbox": sandbox,
    }).artifact("root")
    return ctx.actions.assert_short_path(root, short_path = "root")
