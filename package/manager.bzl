"""Configured native package managers."""

load("//engine:rules.bzl", "EngineInfo", "chroot_run")
load(":release.bzl", "OsReleaseInfo")
load(":repository.bzl", "PackageRepositoryInfo", "merge_repositories", "select_repositories")
load(":system.bzl", "PackageSystemInfo")

PackageManagerInfo = provider(
    doc = "The engine and repository selection used for native package operations.",
    fields = {
        "release": provider_field(Dependency),
        "engine": provider_field(Dependency),
        "package_system": provider_field(Dependency),
        "repositories": provider_field(list[Dependency]),
        "priorities": provider_field(dict[str, int]),
        "solver_caches": provider_field(dict[str, Artifact]),
    },
)

def _package_manager_impl(ctx: AnalysisContext) -> list[Provider]:
    release = ctx.attrs.release[OsReleaseInfo]
    package_system = release.package_system
    system = package_system[PackageSystemInfo]

    repositories = select_repositories(
        release.repository_universe,
        ctx.attrs.enable_repository_groups,
        ctx.attrs.disable_repository_groups,
    )
    repositories = merge_repositories(package_system, repositories + ctx.attrs.additional_repositories)
    by_id = {repository[PackageRepositoryInfo].id: repository for repository in repositories}

    priorities = {
        repository[PackageRepositoryInfo].id: repository[PackageRepositoryInfo].priority
        for repository in repositories
    }
    for rid, priority in ctx.attrs.repository_priorities.items():
        if rid not in by_id:
            fail("priority override names unknown repository '{}'".format(rid))
        priorities[rid] = priority

    engine = ctx.attrs.engine[EngineInfo]
    caches = {}
    for repository in repositories:
        repo = repository[PackageRepositoryInfo]
        cache = ctx.actions.declare_output("solver-cache-{}".format(repo.id), dir = True)
        ctx.actions.run(
            cmd_args(
                chroot_run(engine = engine, exe = system.plan),
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
        PackageManagerInfo(
            release = ctx.attrs.release,
            engine = ctx.attrs.engine,
            package_system = package_system,
            repositories = repositories,
            priorities = priorities,
            solver_caches = caches,
        ),
    ]

_package_manager = rule(
    impl = _package_manager_impl,
    attrs = {
        "release": attrs.dep(providers = [OsReleaseInfo]),
        "engine": attrs.dep(providers = [EngineInfo]),
        "enable_repository_groups": attrs.list(attrs.string(), default = []),
        "disable_repository_groups": attrs.list(attrs.string(), default = []),
        "additional_repositories": attrs.list(
            attrs.dep(providers = [PackageRepositoryInfo]),
            default = [],
        ),
        "repository_priorities": attrs.dict(attrs.string(), attrs.int(), default = {}),
    },
)

def package_manager(name: str, **kwargs) -> None:
    if not name.endswith(".package-manager"):
        fail("package_manager name must end with '.package-manager': {}".format(name))
    _package_manager(
        name = name,
        **kwargs
    )
