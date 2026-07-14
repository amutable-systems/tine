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
        "package_sets": provider_field(dict[str, list[str]]),
        "package_system": provider_field(Dependency),
        "repositories": provider_field(list[Dependency]),
        "priorities": provider_field(dict[str, int]),
        "solver_caches": provider_field(dict[str, Artifact]),
    },
)

def _package_manager_impl(ctx: AnalysisContext) -> list[Provider]:
    if ctx.attrs.base != None:
        if ctx.attrs.release != None or ctx.attrs.engine != None:
            fail("derived package_manager cannot set release or engine")
        if ctx.attrs.enable_repository_groups or ctx.attrs.disable_repository_groups:
            fail("derived package_manager cannot change repository groups")
        base = ctx.attrs.base[PackageManagerInfo]
        release = base.release
        engine_dep = base.engine
        package_system = base.package_system
        package_sets = base.package_sets
        repositories = base.repositories
        priorities = dict(base.priorities)
        caches = dict(base.solver_caches)
    else:
        if ctx.attrs.release == None or ctx.attrs.engine == None:
            fail("package_manager requires release and engine when base is not set")
        release = ctx.attrs.release
        engine_dep = ctx.attrs.engine
        release_info = release[OsReleaseInfo]
        package_system = release_info.package_system
        package_sets = release_info.package_sets
        repositories = select_repositories(
            release_info.repository_universe,
            ctx.attrs.enable_repository_groups,
            ctx.attrs.disable_repository_groups,
        )
        priorities = {}
        caches = {}

    repositories = merge_repositories(package_system, repositories + ctx.attrs.additional_repositories)
    by_id = {repository[PackageRepositoryInfo].id: repository for repository in repositories}

    for repository in repositories:
        repo = repository[PackageRepositoryInfo]
        if repo.id not in priorities:
            priorities[repo.id] = repo.priority
    for rid, priority in ctx.attrs.repository_priorities.items():
        if rid not in by_id:
            fail("priority override names unknown repository '{}'".format(rid))
        priorities[rid] = priority

    engine = engine_dep[EngineInfo]
    system = package_system[PackageSystemInfo]
    for repository in repositories:
        repo = repository[PackageRepositoryInfo]
        if repo.id in caches:
            continue
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
            release = release,
            engine = engine_dep,
            package_sets = package_sets,
            package_system = package_system,
            repositories = repositories,
            priorities = priorities,
            solver_caches = caches,
        ),
    ]

_package_manager = rule(
    impl = _package_manager_impl,
    attrs = {
        "base": attrs.option(attrs.dep(providers = [PackageManagerInfo]), default = None),
        "release": attrs.option(attrs.dep(providers = [OsReleaseInfo]), default = None),
        "engine": attrs.option(attrs.dep(providers = [EngineInfo]), default = None),
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
