"""Format-neutral distribution and package-installation rules."""

load("//engine:rules.bzl", "EngineInfo", "chroot_run")
load(":format.bzl", "PackageFormatInfo")
load(":repository.bzl", "RepoInfo", "download_closure")

# Prefer local builds even when upstream has a newer version.
_EXTRA_REPO_PRIORITY = 50

DistributionInfo = provider(
    doc = "A distribution build target.",
    fields = {
        "engine": provider_field(EngineInfo),
        # provider_field rejects a provider instance as its own type.
        "package_format": provider_field(typing.Any),
        # Dependencies retain format-specific pool providers.
        "buildroot_repositories": provider_field(list[Dependency]),
        "buildroot_base_packages": provider_field(list[str]),
        "solver_caches": provider_field(dict[str, Artifact]),  # repository id -> prebuilt planner cache
    },
)

def _distribution_impl(ctx: AnalysisContext) -> list[Provider]:
    engine = ctx.attrs.engine[EngineInfo]
    fmt = ctx.attrs.package_format[PackageFormatInfo]
    repos = ctx.attrs.buildroot_repositories

    # Share one persistent solver cache per repository across otherwise ephemeral plans.
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
    fmt = ctx.attrs.package_format[PackageFormatInfo]
    create_repository = chroot_run(
        engine = ctx.attrs.engine[EngineInfo],
        exe = fmt.create_repository,
    )
    packages = [p[DefaultInfo].default_outputs[0] for p in ctx.attrs.packages]

    repo_dir = ctx.actions.declare_output("repo", dir = True)
    cmd = cmd_args(create_repository, "--out", repo_dir.as_output())
    for p in packages:
        cmd.add("--package", p)
    ctx.actions.run(cmd, category = "create_repository")

    return [
        DefaultInfo(default_output = repo_dir),
        RepoInfo(id = ctx.attrs.id, dir = repo_dir),
    ]

# Publish package targets as a repository for buildroot resolution.
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

# buildifier: disable=function-docstring-args
# buildifier: disable=function-docstring-return
def remote_repository_base(ctx: AnalysisContext, repo_dir: Artifact) -> list[Provider]:
    """Register the format-neutral interface to a remote repository."""

    # The snapshot subtarget refreshes the authored manifest on the host.
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
    """Generate a repository from local package output directories."""
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

# Share repositories for identical distribution/package inputs.
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
    """Plan, select, and install a fresh root or delta."""
    info = distribution[DistributionInfo]
    repositories = info.buildroot_repositories
    engine = info.engine
    fmt = info.package_format

    # Publish self-hosted outputs in a shared, higher-priority repository.
    extra_repo = None
    if extra_packages:
        extra_repo = ctx.actions.anon_target(_extra_repository, {
            "name": "//extra-repository:{}".format(distribution.label.name),
            "distribution": distribution,
            "packages": extra_packages,
        }).artifact("repo")
        extra_repo = ctx.actions.assert_short_path(extra_repo, short_path = "repo")

    # Resolve the requested closure.
    tx = ctx.actions.declare_output("transaction.json")
    plan = cmd_args(chroot_run(engine = engine, exe = fmt.plan), "solve", "--out", tx.as_output())
    if extra_repo != None:
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

    # Select local outputs or remote pool artifacts from the transaction.
    closure = download_closure(ctx, tx, repositories = repositories, extra_packages = extra_packages)

    # Install the already-resolved closure.
    out = ctx.actions.declare_output("install.delta" if stack else "root", dir = True)
    cmd = cmd_args(
        chroot_run(engine = engine, exe = fmt.install),
        "--packages-dir",
        closure,
        "--target",
        out.as_output(),
    )
    if stack:
        # Overlayfs requires a workdir on the upper's filesystem.
        cmd.add("--work", ctx.actions.declare_output("install.work", dir = True).as_output())
    for lower in stack:
        cmd.add("--lower", lower)
    ctx.actions.run(cmd, category = "assemble")

    return out

def _install_packages_impl(ctx: AnalysisContext) -> list[Provider]:
    root = _install_actions(ctx, ctx.attrs.distribution, ctx.attrs.install, [], ctx.attrs.extra_packages)
    return [DefaultInfo(default_output = root), _RootInfo(root = root)]

# Identical fresh roots share one anonymous target and analysis.
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
    if not stack:
        root = ctx.actions.anon_target(_install_packages, {
            # The hash still distinguishes install sets without obscuring the distribution.
            "name": "//install-packages:{}".format(distribution.label.name),
            "distribution": distribution,
            "install": sorted(install),
            "extra_packages": extra_packages,
        }).artifact("root")
        return ctx.actions.assert_short_path(root, short_path = "root")
    return _install_actions(ctx, distribution, install, stack, extra_packages)
