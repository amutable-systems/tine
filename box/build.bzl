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
load(":runtime.bzl", "BoxInfo", "chroot_run")

_REPOSITORY_PRIORITY = 99

def _configure_repositories(repositories: list[Dependency]) -> list[ConfiguredPackageRepositoryInfo]:
    configured = []
    for repository in repositories:
        repo = repository[PackageRepositoryInfo]
        if repo.dir == None:
            fail("box: repository '{}' has no bootstrap directory".format(repository.label.name))
        if repo.baseurl == None:
            fail("box: repository '{}' has no bootstrap base URL".format(repository.label.name))
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

def _box_impl(ctx: AnalysisContext) -> list[Provider]:
    release = ctx.attrs.release[OsReleaseInfo]
    system = release.package_system[PackageSystemInfo]
    repositories = select_repositories(
        release.repository_universe,
        ctx.attrs.enable_repository_groups,
        ctx.attrs.disable_repository_groups,
    )
    configured_repositories = _configure_repositories(repositories)

    resolver_box = None
    resolve = None
    if ctx.attrs.resolver_box != None:
        resolver_box = ctx.attrs.resolver_box[BoxInfo]
        solver_caches = (
            [
                solver_cache(
                    ctx,
                    ctx.attrs.resolver_box,
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
            box = resolver_box,
            system = system,
            repositories = configured_repositories,
            install = ctx.attrs.packages,
            arch = ctx.attrs.arch,
            solver_caches = solver_caches,
        )

    transaction = ctx.attrs.lock
    if transaction == None:
        if resolve == None:
            fail("box: a missing lock requires resolver_box")
        transaction = ctx.actions.declare_output("transaction.json")
        resolve.add("--out", transaction.as_output())
        ctx.actions.run(resolve, category = "box_resolve")

    # A predecessor installs the transaction directly. Only a root box must first unpack that
    # same closure into an installer-capable chroot, without metadata or scriptlets.
    packages = select_package_artifacts(
        ctx,
        transaction,
        repositories = repositories,
        suffix = system.package_suffix,
    )
    installer_box = resolver_box
    if installer_box == None:
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
        installer_box = BoxInfo(
            arch = ctx.attrs.arch,
            root = chroot1,
            sandbox = ctx.attrs._sandbox,
        )

    # Use the predecessor or bootstrapped root to produce the fully installed box.
    chroot2 = ctx.actions.declare_output("chroot2", dir = True)
    ctx.actions.run(
        cmd_args(
            chroot_run(
                box = installer_box,
                exe = system.install,
            ),
            spec_args(
                ctx.actions,
                "install.spec.json",
                {
                    "arch": ctx.attrs.arch,
                    "box_config": True,
                    "docs": True,
                    "installroot": None,
                    "langs": [],
                    "lower": [],
                    "packages_dir": packages,
                    "target": chroot2.as_output(),
                    "work": None,
                },
            ),
        ),
        category = "box",
    )

    info = BoxInfo(
        arch = ctx.attrs.arch,
        root = chroot2,
        sandbox = ctx.attrs._sandbox,
    )

    # A locked bootstrap box can use its completed root to update its own transaction.
    if resolve == None:
        resolve = solve_command(
            ctx = ctx,
            box = info,
            system = system,
            repositories = configured_repositories,
            install = ctx.attrs.packages,
            arch = ctx.attrs.arch,
        )
    sub_targets = {
        "resolve": [DefaultInfo(), RunInfo(args = resolve)],
        "transaction": [DefaultInfo(default_output = transaction)],
    }

    # Running a box target is an interactive act, so its own RunInfo is the host-integrated relaxed
    # entry. Build actions never reach it: they take BoxInfo and construct their own hermetic
    # chroot_run.
    return [
        DefaultInfo(default_output = chroot2, sub_targets = sub_targets),
        info,
        chroot_run(info, relaxed = True, name = ctx.label.name.removesuffix(".box")),
    ]

_box = rule(
    impl = _box_impl,
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
            doc = "top-level box package names used to resolve the effective transaction",
        ),
        "release": attrs.dep(providers = [OsReleaseInfo], doc = "base OS release for the box root"),
        "resolver_box": attrs.option(
            attrs.dep(providers = [BoxInfo]),
            default = None,
            doc = "predecessor box used to resolve and install this box's transaction",
        ),
        # BoxInfo carries this into the rest of the graph.
        "_sandbox": attrs.exec_dep(default = "tine//box:sandbox", providers = [RunInfo]),
    },
)

def box(name: str, packages: list[str], release: str, labels: list[str] = [], **kwargs) -> None:
    """Declare a box rooted in one base OS release."""
    if not name.endswith(".box"):
        fail("box name must end with '.box': {}".format(name))
    locks = glob(["snapshot/box/" + name[: -len(".box")] + ".json"])
    _box(
        name = name,
        packages = packages,
        release = release,
        labels = ["tine:box"] + labels,
        lock = locks[0] if locks else None,
        **kwargs,
    )
