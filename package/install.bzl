"""Resolve and install native packages into filesystem roots."""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load(":manager.bzl", "PackageManagerInfo", "materialize_local_repository")
load(
    ":repository.bzl",
    "ConfiguredPackageRepositoryInfo",
    "select_package_artifacts",
)
load(":solver.bzl", "solve_command")
load(":system.bzl", "PackageSystemInfo")

_EXTRA_REPO_PRIORITY = 50

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
    configured_repositories = package_manager.repositories
    repositories = []
    for configured in configured_repositories:
        if configured.dependency == None:
            fail("package manager contains inline repository '{}'".format(configured.id))
        repositories.append(configured.dependency)
    engine = package_manager.engine[EngineInfo]
    system = package_manager.package_system[PackageSystemInfo]

    extra_repo = None
    if extra_packages:
        extra_repo = materialize_local_repository(
            ctx,
            package_manager.engine,
            package_manager.package_system,
            extra_packages,
        )

    plan_repositories = []
    if extra_repo != None:
        plan_repositories.append(ConfiguredPackageRepositoryInfo(
            id = "extra",
            directory = extra_repo,
            priority = _EXTRA_REPO_PRIORITY,
        ))
    plan_repositories.extend(configured_repositories)
    tx = ctx.actions.declare_output("transaction.json")
    plan = solve_command(
        ctx = ctx,
        engine = engine,
        system = system,
        repositories = plan_repositories,
        install = install,
        arch = engine.arch,
        output = tx.as_output(),
        solver_caches = package_manager.solver_caches,
        lowers = stack,
    )
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
