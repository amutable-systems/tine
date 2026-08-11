"""Resolve and install native packages into filesystem roots."""

load("//:specs.bzl", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load(":local_packages.bzl", "LocalPackageUniverseInfo", "select_local_packages")
load(":manager.bzl", "PackageManagerInfo", "materialize_local_repository")
load(
    ":repository.bzl",
    "ConfiguredPackageRepositoryInfo",
    "LocalPackageInfo",
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
    extra_packages: list[Artifact] = [],
    local_seed: list[str] | None = None,
    identifier: str | None = None,
) -> Artifact:
    """Plan an install and select its exact package artifacts.

    A manager with local packages offers the seed's runtime closure of locally built packages to
    the solver; `local_seed` widens that seed beyond this request, e.g. to a whole layer stack."""
    package_manager = package_manager_dep[PackageManagerInfo]
    configured_repositories = package_manager.repositories
    repositories = []
    for configured in configured_repositories:
        if configured.dependency == None:
            fail("package manager contains inline repository '{}'".format(configured.id))
        repositories.append(configured.dependency)
    box = package_manager.box[BoxInfo]
    system = package_manager.package_system[PackageSystemInfo]

    if package_manager.local_packages != None:
        if extra_packages:
            fail("resolve_packages: explicit extra packages cannot combine with manager local packages")
        selected = select_local_packages(
            package_manager.local_packages[LocalPackageUniverseInfo],
            local_seed if local_seed != None else install,
        )
        extra_packages = [dep[LocalPackageInfo].packages for dep in selected]

    extra_repo = None
    if extra_packages:
        extra_repo = materialize_local_repository(
            ctx,
            package_manager.box,
            package_manager.package_system,
            extra_packages,
        )

    plan_repositories = []
    if extra_repo != None:
        plan_repositories.append(
            ConfiguredPackageRepositoryInfo(
                id = "extra",
                directory = extra_repo,
                priority = _EXTRA_REPO_PRIORITY,
            )
        )
    plan_repositories.extend(configured_repositories)
    prefix = identifier + "/" if identifier != None else ""
    tx = ctx.actions.declare_output(prefix + "transaction.json")
    plan = solve_command(
        ctx = ctx,
        box = box,
        system = system,
        repositories = plan_repositories,
        install = install,
        arch = box.arch,
        output = tx.as_output(),
        solver_caches = package_manager.solver_caches,
        lowers = stack,
        spec_name = prefix + "solve.spec.json",
    )
    ctx.actions.run(plan, category = "plan", identifier = identifier or "install")

    return select_package_artifacts(
        ctx,
        tx,
        repositories = repositories,
        suffix = system.package_suffix,
        extra_packages = extra_packages,
        name = prefix + "install.closure",
    )

def _install_actions(
    ctx: AnalysisContext,
    package_manager_dep: Dependency,
    install: list[str],
    stack: list[Artifact],
    extra_packages: list[Artifact],
) -> Artifact:
    package_manager = package_manager_dep[PackageManagerInfo]
    system = package_manager.package_system[PackageSystemInfo]
    box = package_manager.box[BoxInfo]
    closure = resolve_packages(ctx, package_manager_dep, install, stack, extra_packages)
    out = ctx.actions.declare_output("install.delta" if stack else "root", dir = True)
    work = ctx.actions.declare_output("install.work", dir = True) if stack else None
    cmd = cmd_args(
        box_run(box = box, exe = system.install),
        spec_args(
            ctx.actions,
            "install.spec.json",
            {
                "arch": box.arch,
                "box_config": False,
                "docs": True,
                "installroot": None,
                "langs": [],
                "lower": stack,
                "packages_dir": closure,
                "target": out.as_output(),
                "work": work.as_output() if work != None else None,
            },
        ),
    )
    ctx.actions.run(cmd, category = "install")
    return out

def _install_packages_impl(ctx: AnalysisContext) -> list[Provider]:
    root = _install_actions(ctx, ctx.attrs.package_manager, ctx.attrs.install, [], ctx.attrs.extra_packages)
    return [DefaultInfo(default_output = root), _RootInfo(root = root)]

_install_packages = anon_rule(
    impl = _install_packages_impl,
    attrs = {
        "extra_packages": attrs.list(attrs.source(), default = []),
        "install": attrs.list(attrs.string()),
        "package_manager": attrs.dep(providers = [PackageManagerInfo]),
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
    extra_packages: list[Artifact] = [],
) -> Artifact:
    if not stack:
        root = ctx.actions.anon_target(
            _install_packages,
            {
                "extra_packages": extra_packages,
                "install": sorted(install),
                "name": "//install-packages:{}".format(package_manager.label.name),
                "package_manager": package_manager,
            },
        ).artifact("root")
        return ctx.actions.assert_short_path(root, short_path = "root")
    return _install_actions(ctx, package_manager, install, stack, extra_packages)
