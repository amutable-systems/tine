"""Distribution rules: the format-neutral machinery //distribution composes."""

load(":engine.bzl", "EngineInfo", "chroot_run")
load(":package_format.bzl", "PackageFormatInfo")

RepoInfo = provider(
    # `dir` is packages + `repodata/` (what the plan resolves against); `packages` are the
    # individual artifacts, keyed by basename to scope an install to its resolved closure.
    doc = "A package repository.",
    fields = {
        "id": provider_field(str),
        "dir": provider_field(Artifact),
        "packages": provider_field(list[Artifact]),
    },
)

DistributionInfo = provider(
    # Pure data (an engine + format plugin + buildroot repos and base packages). The engine
    # root may belong to another distribution (CentOS builds in the Fedora engine root).
    doc = "A distribution build target.",
    fields = {
        "engine": provider_field(EngineInfo),
        # typing.Any, not PackageFormatInfo: provider_field rejects a provider instance under its
        # own type, and reads come back as the generic Provider anyway.
        "package_format": provider_field(typing.Any),
        "buildroot_repositories": provider_field(list[RepoInfo]),
        "buildroot_base_packages": provider_field(list[str]),
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
    # writer lives there); chroot_run runs it at the fixed assembly epoch so the
    # repodata is reproducible.
    fmt = ctx.attrs.package_format[PackageFormatInfo]
    createrepo = chroot_run(engine = ctx.attrs.engine[EngineInfo], exe = fmt.createrepo)
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

_BuildrootInfo = provider(
    doc = "The assembled buildroot tree, carried out of the anon target for its `root` promise.",
    fields = {"root": provider_field(Artifact)},
)

def _buildroot_impl(ctx: AnalysisContext) -> list[Provider]:
    """Install `install`'s resolved closure into a fresh root.

    Resolve `install` over the distribution's repos. Format-neutral: it drives the
    distribution's own plan + install drivers.

    Three actions: (1) plan resolves the install set's runtime closure over the repos →
    a transaction (resolved package filenames); (2) materialize scopes a dir to exactly
    those packages (dynamic, so unrelated repo changes cut off); (3) install lays that
    closure into a fresh root.
    """
    distribution = ctx.attrs.distribution[DistributionInfo]
    repos = distribution.buildroot_repositories
    fmt = distribution.package_format
    engine = distribution.engine

    # Action 1 — plan: resolve the closure over the repos' repodata → a transaction.
    # Re-runs on any repo change, but its output is stable unless *this* closure changed.
    tx = ctx.actions.declare_output("transaction.json")
    plan = cmd_args(
        chroot_run(engine = engine, exe = fmt.plan),
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
            chroot_run(engine = engine, exe = fmt.install),
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
# analysis (and thus one build), not just an action-cache hit. The sandbox rides in on
# the distribution's EngineInfo, sidestepping anon rules' inability to declare exec_dep.
_buildroot = anon_rule(
    impl = _buildroot_impl,
    attrs = {
        "distribution": attrs.dep(providers = [DistributionInfo]),
        "install": attrs.list(attrs.string()),
    },
    artifact_promise_mappings = {
        "root": lambda p: p[_BuildrootInfo].root,
    },
)

def assemble_root(ctx: AnalysisContext, distribution: Dependency, install: list[str]) -> Artifact:
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
    }).artifact("root")
    return ctx.actions.assert_short_path(root, short_path = "root")
