"""Build reusable execution environments and run commands inside them."""

load("//package:repository.bzl", "PackageRepositoryInfo", "RepositoryUniverseInfo", "select_package_artifacts", "select_repositories")
load("//package:system.bzl", "PackageSystemInfo")

ASSEMBLY_SDE = 1739577600

EngineInfo = provider(
    # Carry the sandbox through dependencies because anon rules cannot declare exec_dep.
    doc = "The reusable execution environment: a root plus the sandbox that enters it.",
    fields = {
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

def _engine_impl(ctx: AnalysisContext) -> list[Provider]:
    system = ctx.attrs.package_system[PackageSystemInfo]
    universe = ctx.attrs.repository_universe[RepositoryUniverseInfo]
    if universe.package_system.label != ctx.attrs.package_system.label:
        fail("engine repository universe uses a different package system")
    repositories = select_repositories(
        ctx.attrs.repository_universe,
        ctx.attrs.enable_repository_groups,
        ctx.attrs.disable_repository_groups,
    )

    # Select installable and payload representations from the locked seed transaction.
    packages = select_package_artifacts(ctx, ctx.attrs.lock, repositories = repositories)
    payloads = select_package_artifacts(
        ctx,
        ctx.attrs.lock,
        name = "extract.closure",
        repositories = repositories,
        representation = "payload",
    )

    # Bootstrap chroot1 without an RPM database or scriptlets.
    chroot1 = ctx.actions.declare_output("chroot1", dir = True)
    ctx.actions.run(
        cmd_args(system.extract[RunInfo], chroot1.as_output(), payloads),
        category = "extract",
    )

    # Use chroot1 to produce the fully installed engine.
    chroot2 = ctx.actions.declare_output("chroot2", dir = True)
    ctx.actions.run(
        cmd_args(
            chroot_run(
                engine = EngineInfo(root = chroot1, sandbox = ctx.attrs._sandbox),
                exe = system.install,
            ),
            "--packages-dir",
            packages,
            "--target",
            chroot2.as_output(),
            "--resolv-symlink",
        ),
        category = "engine",
    )

    info = EngineInfo(root = chroot2, sandbox = ctx.attrs._sandbox)

    # Refresh resolves the engine lock against the freshly pinned repositories.
    resolve = cmd_args(chroot_run(engine = info, exe = system.plan), "solve", "--arch", ctx.attrs.arch)
    for repository in repositories:
        repo = repository[PackageRepositoryInfo]
        resolve.add("--repo", cmd_args(repo.dir, format = repo.id + "={}"))
    for package in ctx.attrs.packages:
        resolve.add("--install", package)
    sub_targets = {
        "resolve": [DefaultInfo(), RunInfo(args = resolve)],
    }

    return [
        DefaultInfo(default_output = chroot2, sub_targets = sub_targets),
        info,
        chroot_run(info),
    ]

engine = rule(
    impl = _engine_impl,
    attrs = {
        "packages": attrs.list(
            attrs.string(),
            doc = "top-level engine package names (authored; the lock pins the closure)",
        ),
        "repository_universe": attrs.dep(
            providers = [RepositoryUniverseInfo],
            doc = "repository universe used to resolve the engine",
        ),
        "enable_repository_groups": attrs.list(attrs.string(), default = []),
        "disable_repository_groups": attrs.list(attrs.string(), default = []),
        "package_system": attrs.dep(
            providers = [PackageSystemInfo],
            doc = "the native package-system drivers used to bootstrap the engine",
        ),
        "lock": attrs.source(
            doc = "the @generated engine transaction (source/repo/pkgid/nevra); seed with `{}`",
        ),
        "arch": attrs.string(default = "x86_64", doc = "the resolution arch"),
        # EngineInfo carries this into the rest of the graph.
        "_sandbox": attrs.exec_dep(default = "tine//engine:sandbox", providers = [RunInfo]),
    },
)
