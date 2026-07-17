"""Build reusable execution environments and run commands inside them."""

load("//package:release.bzl", "OsReleaseInfo")
load(
    "//package:repository.bzl",
    "ConfiguredPackageRepositoryInfo",
    "PackageRepositoryInfo",
    "select_package_artifacts",
    "select_repositories",
    "write_repository_manifest",
)
load("//package:system.bzl", "PackageSystemInfo")

ASSEMBLY_SDE = 1739577600
_REPOSITORY_PRIORITY = 99

EngineInfo = provider(
    # Carry the configured sandbox through providers so anonymous targets can reuse it.
    doc = "A reusable execution environment built from one base OS release.",
    fields = {
        "arch": provider_field(str),
        "root": provider_field(Artifact),  # the engine root chroot
        "sandbox": provider_field(Dependency),
    },
)

# buildifier: disable=function-docstring-args
# buildifier: disable=function-docstring-return
def chroot_run(
        engine: EngineInfo,
        exe: Dependency | str | None = None,
        network: bool = False,
        relaxed: bool = False) -> RunInfo:
    """Enter an engine, optionally running a command or interactive relaxed leaf."""
    run = cmd_args(
        engine.sandbox[RunInfo],
        "--tools",
        engine.root,
    )
    if relaxed:
        run.add("--relaxed")
    else:
        run.add("--bind-cwd", "--source-date-epoch", str(ASSEMBLY_SDE))
    if network:
        run.add("--network")
    run.add("--")
    if isinstance(exe, Dependency):
        info = exe[DefaultInfo]
        run.add(cmd_args(info.default_outputs[0], hidden = info.other_outputs))
    elif exe != None:
        run.add(exe)
    return RunInfo(args = run)

def _repository_manifest(ctx: AnalysisContext, repositories: list[Dependency]):
    configured = []
    for repository in repositories:
        repo = repository[PackageRepositoryInfo]
        if repo.dir == None:
            fail("engine: repository '{}' has no bootstrap directory".format(repository.label.name))
        if repo.baseurl == None:
            fail("engine: repository '{}' has no bootstrap base URL".format(repository.label.name))
        configured.append(ConfiguredPackageRepositoryInfo(
            id = repository.label.name,
            dependency = repository,
            directory = repo.dir,
            priority = _REPOSITORY_PRIORITY,
            baseurl = repo.baseurl,
        ))
    return write_repository_manifest(ctx, "repositories.json", configured)

def _resolve_command(
        engine: EngineInfo,
        system: PackageSystemInfo,
        repositories,
        packages: list[str],
        arch: str) -> cmd_args:
    resolve = cmd_args(
        chroot_run(engine = engine, exe = system.plan),
        "solve",
        "--arch",
        arch,
        "--repositories",
        repositories,
    )
    for package in packages:
        resolve.add("--install", package)
    return resolve

def _engine_impl(ctx: AnalysisContext) -> list[Provider]:
    release = ctx.attrs.release[OsReleaseInfo]
    system = release.package_system[PackageSystemInfo]
    repositories = select_repositories(
        release.repository_universe,
        ctx.attrs.enable_repository_groups,
        ctx.attrs.disable_repository_groups,
    )
    repository_manifest = _repository_manifest(ctx, repositories)

    resolver_engine = None
    resolve = None
    if ctx.attrs.resolver_engine != None:
        resolver_engine = ctx.attrs.resolver_engine[EngineInfo]
        resolve = _resolve_command(
            engine = resolver_engine,
            system = system,
            repositories = repository_manifest,
            packages = ctx.attrs.packages,
            arch = ctx.attrs.arch,
        )

    transaction = ctx.attrs.lock
    if transaction == None:
        if resolve == None:
            fail("engine: a missing lock requires resolver_engine")
        transaction = ctx.actions.declare_output("transaction.json")
        ctx.actions.run(
            cmd_args(resolve, "--out", transaction.as_output()),
            category = "engine_resolve",
        )

    # A predecessor installs the transaction directly. Only a root engine must bootstrap an
    # installer-capable chroot from package payloads before it can perform the authoritative install.
    packages = select_package_artifacts(ctx, transaction, repositories = repositories)
    installer_engine = resolver_engine
    if installer_engine == None:
        payloads = select_package_artifacts(
            ctx,
            transaction,
            name = "extract.closure",
            repositories = repositories,
            representation = "payload",
        )
        chroot1 = ctx.actions.declare_output("chroot1", dir = True)
        ctx.actions.run(
            cmd_args(system.extract[RunInfo], chroot1.as_output(), payloads),
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
            "--packages-dir",
            packages,
            "--target",
            chroot2.as_output(),
            "--engine-config",
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
        resolve = _resolve_command(
            engine = info,
            system = system,
            repositories = repository_manifest,
            packages = ctx.attrs.packages,
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
        "enable_repository_groups": attrs.list(attrs.string(), default = []),
        "disable_repository_groups": attrs.list(attrs.string(), default = []),
        "lock": attrs.option(
            attrs.source(),
            default = None,
            doc = "optional frozen transaction, including remote package transport pins",
        ),
        "arch": attrs.string(default = "x86_64", doc = "the resolution arch"),
        # EngineInfo carries this into the rest of the graph.
        "_sandbox": attrs.exec_dep(default = "tine//engine:sandbox", providers = [RunInfo]),
    },
)

def engine(
        name: str,
        packages: list[str],
        release: str,
        **kwargs) -> None:
    """Declare an engine rooted in one base OS release."""
    if not name.endswith(".engine"):
        fail("engine name must end with '.engine': {}".format(name))
    locks = glob(["snapshot/engine/" + name[:-len(".engine")] + ".json"])
    if len(locks) > 1:
        fail("engine {} has multiple locks: {}".format(name, locks))
    _engine(
        name = name,
        packages = packages,
        release = release,
        lock = locks[0] if locks else None,
        **kwargs
    )
