"""Build reusable execution environments."""

load("//:specs.bzl", "spec_args")
load("//package:release.bzl", "OsReleaseInfo")
load(
    "//package:repository.bzl",
    "ConfiguredPackageRepositoryInfo",
    "PackageRepositoryInfo",
    "select_package_artifacts",
    "select_repositories",
)
load("//package:solver.bzl", "solve_command", "solver_cache")
load("//package:system.bzl", "PackageSystemInfo")
load(":runtime.bzl", "EngineInfo", "chroot_run")

_REPOSITORY_PRIORITY = 99

def _configure_repositories(repositories: list[Dependency]) -> list[ConfiguredPackageRepositoryInfo]:
    configured = []
    for repository in repositories:
        repo = repository[PackageRepositoryInfo]
        if repo.dir == None:
            fail("engine: repository '{}' has no bootstrap directory".format(repository.label.name))
        if repo.baseurl == None:
            fail("engine: repository '{}' has no bootstrap base URL".format(repository.label.name))
        configured.append(
            ConfiguredPackageRepositoryInfo(
                id = repository.label.name,
                dependency = repository,
                directory = repo.dir,
                priority = _REPOSITORY_PRIORITY,
                baseurl = repo.baseurl,
            )
        )
    return configured

def _engine_impl(ctx: AnalysisContext) -> list[Provider]:
    release = ctx.attrs.release[OsReleaseInfo]
    system = release.package_system[PackageSystemInfo]
    repositories = select_repositories(
        release.repository_universe,
        ctx.attrs.enable_repository_groups,
        ctx.attrs.disable_repository_groups,
    )
    configured_repositories = _configure_repositories(repositories)

    resolver_engine = None
    resolve = None
    if ctx.attrs.resolver_engine != None:
        resolver_engine = ctx.attrs.resolver_engine[EngineInfo]
        solver_caches = (
            [
                solver_cache(
                    ctx,
                    ctx.attrs.resolver_engine,
                    release.package_system,
                    repository,
                    ctx.attrs.arch,
                )
                for repository in configured_repositories
            ]
            if system.solver_cache
            else []
        )
        resolve = solve_command(
            ctx = ctx,
            engine = resolver_engine,
            system = system,
            repositories = configured_repositories,
            install = ctx.attrs.packages,
            arch = ctx.attrs.arch,
            solver_caches = solver_caches,
        )

    transaction = ctx.attrs.lock
    if transaction == None:
        if resolve == None:
            fail("engine: a missing lock requires resolver_engine")
        transaction = ctx.actions.declare_output("transaction.json")
        resolve.add("--out", transaction.as_output())
        ctx.actions.run(resolve, category = "engine_resolve")

    # A predecessor installs the transaction directly. Only a root engine must first unpack that
    # same closure into an installer-capable chroot, without metadata or scriptlets.
    packages = select_package_artifacts(
        ctx,
        transaction,
        repositories = repositories,
        suffix = system.package_suffix,
    )
    installer_engine = resolver_engine
    if installer_engine == None:
        chroot1 = ctx.actions.declare_output("chroot1", dir = True)
        ctx.actions.run(
            cmd_args(
                system.extract[RunInfo],
                spec_args(
                    ctx.actions,
                    "extract.spec.json",
                    {
                        "out": chroot1.as_output(),
                        "packages": [packages],
                    },
                ),
            ),
            category = "extract",
        )
        installer_engine = EngineInfo(
            arch = ctx.attrs.arch,
            root = chroot1,
            sandbox = ctx.attrs._sandbox,
        )

    # Use the predecessor or bootstrapped root to produce the fully installed engine.
    chroot2 = ctx.actions.declare_output("chroot2", dir = True)
    ctx.actions.run(
        cmd_args(
            chroot_run(
                engine = installer_engine,
                exe = system.install,
            ),
            spec_args(
                ctx.actions,
                "install.spec.json",
                {
                    "arch": ctx.attrs.arch,
                    "docs": True,
                    "engine_config": True,
                    "installroot": None,
                    "langs": [],
                    "lower": [],
                    "packages_dir": packages,
                    "target": chroot2.as_output(),
                    "work": None,
                },
            ),
        ),
        category = "engine",
    )

    info = EngineInfo(
        arch = ctx.attrs.arch,
        root = chroot2,
        sandbox = ctx.attrs._sandbox,
    )

    # A locked bootstrap engine can use its completed root to update its own transaction.
    if resolve == None:
        resolve = solve_command(
            ctx = ctx,
            engine = info,
            system = system,
            repositories = configured_repositories,
            install = ctx.attrs.packages,
            arch = ctx.attrs.arch,
        )
    sub_targets = {
        "resolve": [DefaultInfo(), RunInfo(args = resolve)],
        "transaction": [DefaultInfo(default_output = transaction)],
    }

    return [
        DefaultInfo(default_output = chroot2, sub_targets = sub_targets),
        info,
        chroot_run(info),
    ]

_engine = rule(
    impl = _engine_impl,
    attrs = {
        "arch": attrs.string(default = "x86_64", doc = "the resolution arch"),
        "disable_repository_groups": attrs.list(attrs.string(), default = []),
        "enable_repository_groups": attrs.list(attrs.string(), default = []),
        "labels": attrs.list(attrs.string(), default = []),
        "lock": attrs.option(
            attrs.source(),
            default = None,
            doc = "optional frozen transaction, including remote package transport pins",
        ),
        "packages": attrs.list(
            attrs.string(),
            doc = "top-level engine package names used to resolve the effective transaction",
        ),
        "release": attrs.dep(providers = [OsReleaseInfo], doc = "base OS release for the engine root"),
        "resolver_engine": attrs.option(
            attrs.dep(providers = [EngineInfo]),
            default = None,
            doc = "predecessor engine used to resolve and install this engine's transaction",
        ),
        # EngineInfo carries this into the rest of the graph.
        "_sandbox": attrs.exec_dep(default = "tine//engine:sandbox", providers = [RunInfo]),
    },
)

def engine(name: str, packages: list[str], release: str, labels: list[str] = [], **kwargs) -> None:
    """Declare an engine rooted in one base OS release."""
    if not name.endswith(".engine"):
        fail("engine name must end with '.engine': {}".format(name))
    locks = glob(["snapshot/engine/" + name[: -len(".engine")] + ".json"])
    if len(locks) > 1:
        fail("engine {} has multiple locks: {}".format(name, locks))
    _engine(
        name = name,
        packages = packages,
        release = release,
        labels = ["tine:engine"] + labels,
        lock = locks[0] if locks else None,
        **kwargs,
    )
