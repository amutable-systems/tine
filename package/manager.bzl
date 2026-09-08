"""Configured native package managers."""

load("//:specs.bzl", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//distribution:defs.bzl", "distribution")
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
load(":verify.bzl", "repository_packages")

_LOCAL_REPOSITORY_PRIORITY = 50
_REMOTE_REPOSITORY_PRIORITY = 99

def _materialize_local_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    system = ctx.attrs.package_system[PackageSystemInfo]
    repo = ctx.actions.declare_output("repo", dir = True)
    index = cmd_args(
        box_run(box = ctx.attrs.box[BoxInfo], exe = system.index),
        spec_args(
            ctx.actions,
            "index.spec.json",
            {
                "out": repo.as_output(),
                "packages": [],
                "packages_dirs": ctx.attrs.package_dirs,
            },
        ),
    )
    ctx.actions.run(index, category = "repository_index")
    return [DefaultInfo(default_output = repo)]

_materialize_local_repository = anon_rule(
    impl = _materialize_local_repository_impl,
    attrs = {
        "box": attrs.dep(providers = [BoxInfo]),
        "package_dirs": attrs.list(attrs.source()),
        "package_system": attrs.dep(providers = [PackageSystemInfo]),
    },
    artifact_promise_mappings = {
        "repo": lambda p: p[DefaultInfo].default_outputs[0],
    },
)

def materialize_local_repository(
    ctx: AnalysisContext,
    box: Dependency,
    package_system: Dependency,
    package_dirs: list[Artifact],
) -> Artifact:
    """Materialize local package directories in their consumer's execution context."""
    return ctx.actions.anon_target(
        _materialize_local_repository,
        {
            "box": box,
            "name": "//local-repository:materialize",
            "package_dirs": package_dirs,
            "package_system": package_system,
        },
    ).artifact("repo")

PackageManagerInfo = provider(
    doc = "The box and repository selection used for native package operations.",
    fields = {
        "box": provider_field(Dependency),
        "local_packages": provider_field(Dependency | None, default = None),
        "package_sets": provider_field(dict[str, list[str]]),
        "package_system": provider_field(Dependency),
        "repositories": provider_field(list[ConfiguredPackageRepositoryInfo]),
        "solver_caches": provider_field(list[Artifact]),
    },
)

def _package_manager_impl(ctx: AnalysisContext) -> list[Provider]:
    if ctx.attrs.base != None:
        if ctx.attrs.release != None or ctx.attrs.box != None:
            fail("derived package_manager cannot set release or box")
        if ctx.attrs.enable_repository_groups or ctx.attrs.disable_repository_groups:
            fail("derived package_manager cannot change repository groups")
        base = ctx.attrs.base[PackageManagerInfo]
        box_dep = base.box
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
        if ctx.attrs.release == None or ctx.attrs.box == None:
            fail("package_manager requires release and box when base is not set")
        box_dep = ctx.attrs.box
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

    box = box_dep[BoxInfo]
    configured_repositories = []
    for repository in repositories:
        repo = repository[PackageRepositoryInfo]
        rid = repository.label.name
        configured = configured_by_id.get(rid)
        local = repository.get(LocalPackageRepositoryInfo)
        packages = None
        if configured != None:
            directory = configured.directory
            default_priority = configured.priority
            baseurl = configured.baseurl
            packages = configured.packages
        else:
            default_priority = _LOCAL_REPOSITORY_PRIORITY if local != None else _REMOTE_REPOSITORY_PRIORITY
            if local != None:
                directory = materialize_local_repository(
                    ctx,
                    box_dep,
                    package_system,
                    local.package_dirs,
                )
                baseurl = None
            elif repo.dir != None:
                directory = repo.dir
                baseurl = repo.baseurl
                if baseurl == None:
                    fail("remote repository '{}' has no base URL".format(rid))
                packages = repository_packages(ctx, box, repository)
            else:
                fail("package_manager: repository '{}' has no directory".format(rid))
        priority = ctx.attrs.repository_priorities.get(rid, default_priority)
        configured = ConfiguredPackageRepositoryInfo(
            id = rid,
            dependency = repository,
            directory = directory,
            priority = priority,
            baseurl = baseurl,
            packages = packages,
        )
        if rid not in configured_by_id and package_system[PackageSystemInfo].solver_cache:
            solver_caches.append(
                solver_cache(
                    ctx,
                    box_dep,
                    package_system,
                    configured,
                    box.arch,
                )
            )
        configured_repositories.append(configured)

    return [
        DefaultInfo(),
        PackageManagerInfo(
            box = box_dep,
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
        "additional_repositories": attrs.list(
            attrs.dep(providers = [PackageRepositoryInfo]),
            default = [],
        ),
        "base": attrs.option(attrs.dep(providers = [PackageManagerInfo]), default = None),
        "box": attrs.option(attrs.dep(providers = [BoxInfo]), default = None),
        "disable_repository_groups": attrs.list(attrs.string(), default = []),
        "enable_repository_groups": attrs.list(attrs.string(), default = []),
        "local_packages": attrs.option(
            attrs.dep(providers = [LocalPackageUniverseInfo]),
            default = None,
            doc = "locally built packages preferred over repository packages during installs",
        ),
        "release": attrs.option(attrs.dep(providers = [OsReleaseInfo]), default = None),
        "repository_priorities": attrs.dict(attrs.string(), attrs.int(), default = {}),
    },
)

def package_manager(name: str, **kwargs) -> None:
    if not name.endswith(".package-manager"):
        fail("package_manager name must end with '.package-manager': {}".format(name))

    # An image reaches this through a dependency and configures it on the way, so it takes the
    # package's compatibility without the per-distribution aliases a named target needs.
    _package_manager(name = name, **(distribution.attrs() | kwargs))
