"""Distribution rules: the format-neutral machinery //distribution composes."""

load(":engine.bzl", "EngineInfo", "chroot_run")
load(":package_format.bzl", "PackageFormatInfo")
load(":repo.bzl", "RepoInfo", "download_closure")

# Prefer extra-packages (our own builds) over the upstream repos even when
# upstream carries a newer version that we didn't import/merge yet.
_EXTRA_REPO_PRIORITY = 50

DistributionInfo = provider(
    # An engine + format plugin + buildroot repositories and base packages, plus each
    # repository's prebuilt solver cache. The engine may belong to another distribution.
    doc = "A distribution build target.",
    fields = {
        "engine": provider_field(EngineInfo),
        # typing.Any, not PackageFormatInfo: provider_field rejects a provider instance under its
        # own type, and reads come back as the generic Provider anyway.
        "package_format": provider_field(typing.Any),
        # Keep the dependency, not just RepoInfo: format-specific consumers can reach the
        # repository target's pool providers and their derived representations.
        "buildroot_repositories": provider_field(list[Dependency]),
        "buildroot_base_packages": provider_field(list[str]),
        "solver_caches": provider_field(dict[str, Artifact]),  # repository id -> prebuilt planner cache
    },
)

def _distribution_impl(ctx: AnalysisContext) -> list[Provider]:
    engine = ctx.attrs.engine[EngineInfo]
    fmt = ctx.attrs.package_format[PackageFormatInfo]
    repos = ctx.attrs.buildroot_repositories

    # Loading a repository's metadata is expensive and would be re-paid by every plan
    # action because the sandbox cache is ephemeral. Prebuild each solver cache once
    # in the engine root and let every plan seed from it. Declared on the distribution
    # so all its consumers share one cache build per repository; the actions run only
    # when a plan actually demands them.
    caches = {}
    for repository in repos:
        repo = repository[RepoInfo]
        cache = ctx.actions.declare_output("solver-cache-{}".format(repo.id), dir = True)
        ctx.actions.run(
            cmd_args(
                chroot_run(engine = engine, exe = fmt.plan),
                "make-cache",
                "--repo",
                cmd_args(repo.dir, format = repo.id + "={}"),
                "--out",
                cache.as_output(),
            ),
            category = "solver_cache",
            identifier = repo.id,
        )
        caches[repo.id] = cache

    return [
        DefaultInfo(),
        DistributionInfo(
            engine = engine,
            package_format = fmt,
            buildroot_repositories = repos,
            buildroot_base_packages = ctx.attrs.buildroot_base_packages,
            solver_caches = caches,
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

def _local_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    # Bind the format's repository writer to run in the engine root. chroot_run uses
    # the fixed assembly epoch so the metadata is reproducible.
    fmt = ctx.attrs.package_format[PackageFormatInfo]
    create_repository = chroot_run(
        engine = ctx.attrs.engine[EngineInfo],
        exe = fmt.create_repository,
    )
    packages = [p[DefaultInfo].default_outputs[0] for p in ctx.attrs.packages]

    # Generate repository metadata so the format's planner can resolve against the
    # packages as a real local repository.
    repo_dir = ctx.actions.declare_output("repo", dir = True)
    cmd = cmd_args(create_repository, "--out", repo_dir.as_output())
    for p in packages:
        cmd.add("--package", p)
    ctx.actions.run(cmd, category = "create_repository")

    return [
        DefaultInfo(default_output = repo_dir),
        RepoInfo(id = ctx.attrs.id, dir = repo_dir),
    ]

# A local repository: generate metadata (in an engine root) from a list of package targets
# into a real repository addressed by `id`, also exposing the per-package artifacts. This is
# how our own builds are served to a buildroot resolve.
local_repository = rule(
    impl = _local_repository_impl,
    attrs = {
        "id": attrs.string(doc = "the repo id"),
        "packages": attrs.list(attrs.dep(), doc = "the packages this repo publishes (one dep each)"),
        "engine": attrs.dep(
            providers = [EngineInfo],
            doc = "the engine whose root the repository writer runs in",
        ),
        "package_format": attrs.dep(
            providers = [PackageFormatInfo],
            doc = "the package format plugin supplying the repository writer",
        ),
    },
)

def remote_repository_base(ctx: AnalysisContext, repo_dir: Artifact) -> list[Provider]:
    """Register the format-neutral interface to a remote repository."""

    # The refresh half, as sub-targets: `[manifest]` is the authored data as JSON (the
    # snapshot driver's input, also handed to distribution resolvers), `[snapshot]`
    # runs the driver to (re)pin the metadata (host — pinning is pure fetch-and-filter).
    rid = ctx.label.name
    fmt = ctx.attrs.package_format[PackageFormatInfo]
    manifest = ctx.actions.write("manifest.json", json.encode({"baseurl": ctx.attrs.baseurl, "id": rid}))
    sub_targets = {
        "manifest": [DefaultInfo(default_output = manifest)],
        "snapshot": [DefaultInfo(), RunInfo(args = cmd_args(fmt.snapshot[RunInfo], "--manifest", manifest))],
    }

    return [
        DefaultInfo(default_output = repo_dir, sub_targets = sub_targets),
        RepoInfo(id = rid, dir = repo_dir, priority = ctx.attrs.priority),
    ]

_ExtraRepoInfo = provider(
    doc = "The extra-packages repository tree, carried out of the anon target for its promise.",
    fields = {"repo": provider_field(Artifact)},
)

def _extra_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    """Generate a local repository from our own package output directories.

    Each package location is its output-directory index plus basename, so a consumer can map
    the local transaction entry back to the originating input directory (see repo.bzl)."""
    distribution = ctx.attrs.distribution[DistributionInfo]
    repo = ctx.actions.declare_output("repo", dir = True)
    create_repository = cmd_args(
        chroot_run(
            engine = distribution.engine,
            exe = distribution.package_format.create_repository,
        ),
        "--out",
        repo.as_output(),
    )
    for package_dir in ctx.attrs.packages:
        create_repository.add("--packages-dir", package_dir)
    ctx.actions.run(create_repository, category = "create_repository")
    return [DefaultInfo(default_output = repo), _ExtraRepoInfo(repo = repo)]

# The repo is pure data — (distribution, packages) fully determine it — so it's an anon
# target: roots with different install sets but the same extra packages share one
# metadata build, just as the install root itself is shared (see _install_packages).
_extra_repository = anon_rule(
    impl = _extra_repository_impl,
    attrs = {
        "distribution": attrs.dep(providers = [DistributionInfo]),
        "packages": attrs.list(attrs.source()),
    },
    artifact_promise_mappings = {
        "repo": lambda p: p[_ExtraRepoInfo].repo,
    },
)

_RootInfo = provider(
    doc = "The installed root tree, carried out of the anon target for its `root` promise.",
    fields = {"root": provider_field(Artifact)},
)

def _install_actions(
        ctx: AnalysisContext,
        distribution: Dependency,
        install: list[str],
        stack: list[Artifact],
        extra_packages: list[Artifact]) -> Artifact:
    """The plan → download → install action triple; returns the fresh root or the delta.

    Format-neutral: drives the distribution's own plan + install drivers, selecting the plan's
    identities from repository-owned pools in between. `extra_packages` (our own builds) are
    published in a priority extra repository (Action 0) so the resolve prefers them over upstream.
    With a `stack` (an existing tree as overlay deltas, bottom..top), the plan resolves against
    the merged installed set too — only the increment is fetched — and the install lands as a
    new delta over it."""
    info = distribution[DistributionInfo]
    repositories = info.buildroot_repositories
    engine = info.engine
    fmt = info.package_format

    # Action 0 (self-hosting only) — publish our own build outputs in a local extra-packages
    # repository, via a nested anon target so roots with different install sets but identical
    # extra packages share it. Its lower priority number outranks the upstream repositories,
    # so the plan takes our build for any capability we provide, even when upstream carries
    # a newer version we have not imported yet.
    extra_repo = None
    if extra_packages:
        extra_repo = ctx.actions.anon_target(_extra_repository, {
            "name": "//extra-repository:{}".format(distribution.label.name),
            "distribution": distribution,
            "packages": extra_packages,
        }).artifact("repo")
        extra_repo = ctx.actions.assert_short_path(extra_repo, short_path = "repo")

    # Action 1 — resolve the closure over the repositories' metadata into a transaction.
    # Re-runs on any metadata change, but its output is stable unless this closure changed.
    tx = ctx.actions.declare_output("transaction.json")
    plan = cmd_args(chroot_run(engine = engine, exe = fmt.plan), "solve", "--out", tx.as_output())
    if extra_repo != None:
        # Our builds resolve from an explicit local repository; Action 2 projects them in place.
        plan.add("--repo", cmd_args(extra_repo, format = "extra={}"))
        plan.add("--local-repo", "extra")
        plan.add("--priority", "extra={}".format(_EXTRA_REPO_PRIORITY))
    for repository in repositories:
        repo = repository[RepoInfo]
        plan.add("--repo", cmd_args(repo.dir, format = repo.id + "={}"))
        plan.add("--priority", "{}={}".format(repo.id, repo.priority))
        plan.add("--cache", info.solver_caches[repo.id])
    for lower in stack:
        plan.add("--lower", lower)
    for cap in install:
        plan.add("--install", cap)
    ctx.actions.run(plan, category = "plan")

    # Action 2 — select the resolved transaction. Extra packages are projected in place from
    # their output directories; remote packages come from the repository pool.
    closure = download_closure(ctx, tx, repositories = repositories, extra_packages = extra_packages)

    # Action 3 — install that closure (it's already the exact set). Cuts off when the
    # closure is unchanged.
    out = ctx.actions.declare_output("install.delta" if stack else "root", dir = True)
    cmd = cmd_args(
        chroot_run(engine = engine, exe = fmt.install),
        "--packages-dir",
        closure,
        "--target",
        out.as_output(),
    )
    if stack:
        # overlayfs leaves the workdir dirty; declared so it lands in buck-out (on the
        # upper's filesystem) and buck tracks it.
        cmd.add("--work", ctx.actions.declare_output("install.work", dir = True).as_output())
    for lower in stack:
        cmd.add("--lower", lower)
    ctx.actions.run(cmd, category = "assemble")

    return out

def _install_packages_impl(ctx: AnalysisContext) -> list[Provider]:
    root = _install_actions(ctx, ctx.attrs.distribution, ctx.attrs.install, [], ctx.attrs.extra_packages)
    return [DefaultInfo(default_output = root), _RootInfo(root = root)]

# A fresh install root (a package buildroot or an image base layer) is pure data —
# (distribution, install set, extra packages) fully determine it — so it's an anon target: two
# consumers with the same distribution and install set share one analysis (and thus one build),
# not just an action-cache hit. The sandbox rides in on the distribution's EngineInfo,
# sidestepping anon rules' inability to declare exec_dep.
_install_packages = anon_rule(
    impl = _install_packages_impl,
    attrs = {
        "distribution": attrs.dep(providers = [DistributionInfo]),
        "install": attrs.list(attrs.string()),
        "extra_packages": attrs.list(attrs.source(), default = []),
    },
    artifact_promise_mappings = {
        "root": lambda p: p[_RootInfo].root,
    },
)

def install_packages(
        ctx: AnalysisContext,
        distribution: Dependency,
        install: list[str],
        stack: list[Artifact] = [],
        extra_packages: list[Artifact] = []) -> Artifact:
    """Plan + install `install`, returning a fresh full root or an incremental delta.

    A fresh root goes through the shared `_install_packages` anon target (see there): `install`
    is sorted so equal sets share, and `assert_short_path` pins the promise's short path (the anon
    output's own — a shared target can't take a per-caller name) so it's usable before the anon
    target is analyzed. `extra_packages` (our own package output directories, which outrank the
    upstream repositories; empty for a pure-seed root) keys the anon target too, so distinct sets
    don't collide but identical triples still share. An incremental install is keyed on the
    caller's own `stack`, so there's nothing to share — the actions run inline."""
    if not stack:
        root = ctx.actions.anon_target(_install_packages, {
            # A friendlier log label than the default `anon//:_install_packages@<hash>`. Derived
            # purely from the distribution (already a key attr), so it doesn't split sharing;
            # the `@<hash>` buck appends still distinguishes distinct install sets.
            "name": "//install-packages:{}".format(distribution.label.name),
            "distribution": distribution,
            "install": sorted(install),
            "extra_packages": extra_packages,
        }).artifact("root")
        return ctx.actions.assert_short_path(root, short_path = "root")
    return _install_actions(ctx, distribution, install, stack, extra_packages)
