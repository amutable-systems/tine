"""Resolve and install native packages into filesystem roots."""

load("//engine:rules.bzl", "EngineInfo", "chroot_run")
load(":manager.bzl", "PackageManagerInfo")
load(":repository.bzl", "LocalPackageRepositoryInfo", "PackageRepositoryInfo", "select_package_artifacts")
load(":system.bzl", "PackageSystemInfo")

_EXTRA_REPO_PRIORITY = 50

_ExtraRepoInfo = provider(
    doc = "The extra-packages repository tree carried out of an anonymous target.",
    fields = {"repo": provider_field(Artifact)},
)

def _extra_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    package_manager = ctx.attrs.package_manager[PackageManagerInfo]
    system = package_manager.package_system[PackageSystemInfo]
    repo = ctx.actions.declare_output("repo", dir = True)
    createrepo = cmd_args(
        chroot_run(engine = package_manager.engine[EngineInfo], exe = system.createrepo),
        "--out",
        repo.as_output(),
    )
    for package_dir in ctx.attrs.packages:
        createrepo.add("--packages-dir", package_dir)
    ctx.actions.run(createrepo, category = "createrepo")
    return [DefaultInfo(default_output = repo), _ExtraRepoInfo(repo = repo)]

_extra_repository = anon_rule(
    impl = _extra_repository_impl,
    attrs = {
        "package_manager": attrs.dep(providers = [PackageManagerInfo]),
        "packages": attrs.list(attrs.source()),
    },
    artifact_promise_mappings = {
        "repo": lambda p: p[_ExtraRepoInfo].repo,
    },
)

_RootInfo = provider(
    doc = "The installed root tree carried out of an anonymous target.",
    fields = {"root": provider_field(Artifact)},
)

def resolve_packages(
        ctx: AnalysisContext,
        package_manager_dep: Dependency,
        install: list[str],
        stack: list[Artifact],
        extra_packages: list[Artifact] = []) -> Artifact:
    """Plan an install and select its exact package artifacts."""
    package_manager = package_manager_dep[PackageManagerInfo]
    repositories = package_manager.repositories
    system = package_manager.package_system[PackageSystemInfo]

    extra_repo = None
    if extra_packages:
        extra_repo = ctx.actions.anon_target(_extra_repository, {
            "name": "//extra-repository:{}".format(package_manager_dep.label.name),
            "package_manager": package_manager_dep,
            "packages": extra_packages,
        }).artifact("repo")
        extra_repo = ctx.actions.assert_short_path(extra_repo, short_path = "repo")

    tx = ctx.actions.declare_output("transaction.json")
    plan = cmd_args(
        chroot_run(engine = package_manager.engine[EngineInfo], exe = system.plan),
        "solve",
        "--out",
        tx.as_output(),
    )
    if extra_repo != None:
        plan.add("--repo", cmd_args(extra_repo, format = "extra={}"))
        plan.add("--local-repo", "extra")
        plan.add("--priority", "extra={}".format(_EXTRA_REPO_PRIORITY))
    for repository in repositories:
        repo = repository[PackageRepositoryInfo]
        plan.add("--repo", cmd_args(repo.dir, format = repo.id + "={}"))
        priority = package_manager.priorities.get(repo.id, repo.priority)
        plan.add("--priority", "{}={}".format(repo.id, priority))
        if repository.get(LocalPackageRepositoryInfo) != None:
            plan.add("--local-repo", repo.id)
        cache = package_manager.solver_caches.get(repo.id)
        if cache != None:
            plan.add("--cache", cache)
    for lower in stack:
        plan.add("--lower", lower)
    for cap in install:
        plan.add("--install", cap)
    ctx.actions.run(plan, category = "plan")

    return select_package_artifacts(ctx, tx, repositories = repositories, extra_packages = extra_packages)

def _install_actions(
        ctx: AnalysisContext,
        package_manager_dep: Dependency,
        install: list[str],
        stack: list[Artifact],
        extra_packages: list[Artifact]) -> Artifact:
    package_manager = package_manager_dep[PackageManagerInfo]
    system = package_manager.package_system[PackageSystemInfo]
    closure = resolve_packages(ctx, package_manager_dep, install, stack, extra_packages)
    out = ctx.actions.declare_output("install.delta" if stack else "root", dir = True)
    cmd = cmd_args(
        chroot_run(engine = package_manager.engine[EngineInfo], exe = system.install),
        "--packages-dir",
        closure,
        "--target",
        out.as_output(),
    )
    if stack:
        cmd.add("--work", ctx.actions.declare_output("install.work", dir = True).as_output())
    for lower in stack:
        cmd.add("--lower", lower)
    ctx.actions.run(cmd, category = "install")
    return out

def _install_packages_impl(ctx: AnalysisContext) -> list[Provider]:
    root = _install_actions(ctx, ctx.attrs.package_manager, ctx.attrs.install, [], ctx.attrs.extra_packages)
    return [DefaultInfo(default_output = root), _RootInfo(root = root)]

_install_packages = anon_rule(
    impl = _install_packages_impl,
    attrs = {
        "package_manager": attrs.dep(providers = [PackageManagerInfo]),
        "install": attrs.list(attrs.string()),
        "extra_packages": attrs.list(attrs.source(), default = []),
    },
    artifact_promise_mappings = {
        "root": lambda p: p[_RootInfo].root,
    },
)

def install_packages(
        ctx: AnalysisContext,
        package_manager: Dependency,
        install: list[str],
        stack: list[Artifact] = [],
        extra_packages: list[Artifact] = []) -> Artifact:
    if not stack:
        root = ctx.actions.anon_target(_install_packages, {
            "name": "//install-packages:{}".format(package_manager.label.name),
            "package_manager": package_manager,
            "install": sorted(install),
            "extra_packages": extra_packages,
        }).artifact("root")
        return ctx.actions.assert_short_path(root, short_path = "root")
    return _install_actions(ctx, package_manager, install, stack, extra_packages)
