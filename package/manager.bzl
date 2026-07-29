"""Configured native package managers."""

load("//:specs.bzl", "spec_args")
load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load(":local_packages.bzl", "LocalPackageUniverseInfo")
load(":release.bzl", "OsReleaseInfo")
load(
    ":repository.bzl",
    "ConfiguredPackageRepositoryInfo",
    "LocalPackageRepositoryInfo",
    "PackageRepositoryInfo",
    "merge_repositories",
    "select_repositories",
)
load(":solver.bzl", "solver_cache")
load(":system.bzl", "PackageSystemInfo")

_LOCAL_REPOSITORY_PRIORITY = 50
_REMOTE_REPOSITORY_PRIORITY = 99

def _materialize_local_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    system = ctx.attrs.package_system[PackageSystemInfo]
    repo = ctx.actions.declare_output("repo", dir = True)
    index = cmd_args(
        chroot_run(engine = ctx.attrs.engine[EngineInfo], exe = system.index),
        spec_args(ctx, "index.spec.json", {
            "out": repo.as_output(),
            "packages": [],
            "packages_dirs": ctx.attrs.package_dirs,
        }),
    )
    ctx.actions.run(index, category = "repository_index")
    return [DefaultInfo(default_output = repo)]

_materialize_local_repository = anon_rule(
    impl = _materialize_local_repository_impl,
    attrs = {
        "engine": attrs.dep(providers = [EngineInfo]),
        "package_system": attrs.dep(providers = [PackageSystemInfo]),
        "package_dirs": attrs.list(attrs.source()),
    },
    artifact_promise_mappings = {
        "repo": lambda p: p[DefaultInfo].default_outputs[0],
    },
)

def materialize_local_repository(
        ctx: AnalysisContext,
        engine: Dependency,
        package_system: Dependency,
        package_dirs: list[Artifact]) -> Artifact:
    """Materialize local package directories in their consumer's execution context."""
    return ctx.actions.anon_target(_materialize_local_repository, {
        "name": "//local-repository:materialize",
        "engine": engine,
        "package_system": package_system,
        "package_dirs": package_dirs,
    }).artifact("repo")

PackageManagerInfo = provider(
    doc = "The engine and repository selection used for native package operations.",
    fields = {
        "engine": provider_field(Dependency),
        "package_sets": provider_field(dict[str, list[str]]),
        "package_system": provider_field(Dependency),
        "repositories": provider_field(list[ConfiguredPackageRepositoryInfo]),
        "solver_caches": provider_field(list[Artifact]),
        "local_packages": provider_field(Dependency | None, default = None),
    },
)

def _package_manager_impl(ctx: AnalysisContext) -> list[Provider]:
    if ctx.attrs.base != None:
        if ctx.attrs.release != None or ctx.attrs.engine != None:
            fail("derived package_manager cannot set release or engine")
        if ctx.attrs.enable_repository_groups or ctx.attrs.disable_repository_groups:
            fail("derived package_manager cannot change repository groups")
        base = ctx.attrs.base[PackageManagerInfo]
        engine_dep = base.engine
        package_system = base.package_system
        package_sets = base.package_sets
        configured_by_id = {configured.id: configured for configured in base.repositories}
        repositories = []
        for configured in base.repositories:
            if configured.dependency == None:
                fail("package_manager base contains inline repository '{}'".format(configured.id))
            repositories.append(configured.dependency)
        solver_caches = list(base.solver_caches)
        local_packages = ctx.attrs.local_packages if ctx.attrs.local_packages != None else base.local_packages
    else:
        if ctx.attrs.release == None or ctx.attrs.engine == None:
            fail("package_manager requires release and engine when base is not set")
        engine_dep = ctx.attrs.engine
        release_info = ctx.attrs.release[OsReleaseInfo]
        package_system = release_info.package_system
        package_sets = release_info.package_sets
        repositories = select_repositories(
            release_info.repository_universe,
            ctx.attrs.enable_repository_groups,
            ctx.attrs.disable_repository_groups,
        )
        configured_by_id = {}
        solver_caches = []
        local_packages = ctx.attrs.local_packages

    repositories = merge_repositories(package_system, repositories + ctx.attrs.additional_repositories)
    by_id = {repository.label.name: repository for repository in repositories}

    # 'extra' is synthesized per install for buildroot_deps and local-package selection.
    if "extra" in by_id:
        fail("package_manager: repository id 'extra' is reserved for extra-package selection")

    for rid in ctx.attrs.repository_priorities:
        if rid not in by_id:
            fail("priority override names unknown repository '{}'".format(rid))

    engine = engine_dep[EngineInfo]
    configured_repositories = []
    for repository in repositories:
        repo = repository[PackageRepositoryInfo]
        rid = repository.label.name
        configured = configured_by_id.get(rid)
        local = repository.get(LocalPackageRepositoryInfo)
        if configured != None:
            directory = configured.directory
            default_priority = configured.priority
            baseurl = configured.baseurl
        else:
            default_priority = _LOCAL_REPOSITORY_PRIORITY if local != None else _REMOTE_REPOSITORY_PRIORITY
            if local != None:
                directory = materialize_local_repository(
                    ctx,
                    engine_dep,
                    package_system,
                    local.package_dirs,
                )
                baseurl = None
            elif repo.dir != None:
                directory = repo.dir
                baseurl = repo.baseurl
                if baseurl == None:
                    fail("remote repository '{}' has no base URL".format(rid))
            else:
                fail("package_manager: repository '{}' has no directory".format(rid))
        priority = ctx.attrs.repository_priorities.get(rid, default_priority)
        configured = ConfiguredPackageRepositoryInfo(
            id = rid,
            dependency = repository,
            directory = directory,
            priority = priority,
            baseurl = baseurl,
        )
        if rid not in configured_by_id:
            solver_caches.append(solver_cache(
                ctx,
                engine_dep,
                package_system,
                configured,
                engine.arch,
            ))
        configured_repositories.append(configured)

    return [
        DefaultInfo(),
        PackageManagerInfo(
            engine = engine_dep,
            package_sets = package_sets,
            package_system = package_system,
            repositories = configured_repositories,
            solver_caches = solver_caches,
            local_packages = local_packages,
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
        "local_packages": attrs.option(
            attrs.dep(providers = [LocalPackageUniverseInfo]),
            default = None,
            doc = "locally built packages preferred over repository packages during installs",
        ),
    },
)

def package_manager(name: str, **kwargs) -> None:
    if not name.endswith(".package-manager"):
        fail("package_manager name must end with '.package-manager': {}".format(name))
    _package_manager(
        name = name,
        **kwargs
    )
