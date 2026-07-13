"""The engine: the reusable execution environment and how to run commands in it.

`engine` builds one (seed packages → installed chroot); `engine_cmd` is the generic
enter-the-engine prefix (also an engine target's own RunInfo, so `buck run
catalog//:<e>.engine -- <cmd>` gives a shell in the engine); `chroot_run` binds an
executable target to an engine at the fixed assembly epoch.
"""

load(":package_format.bzl", "PackageFormatInfo")
load(":repo.bzl", "RepoInfo", "download_closure")

ASSEMBLY_SDE = 1739577600

EngineInfo = provider(
    # The engine knows how to run things in itself: `sandbox` is the exec-configured chroot
    # launcher, carried here so nothing else needs a `_sandbox` attr (and so it reaches anon
    # rules, which can't declare exec_dep, through their deps).
    doc = "The reusable execution environment: a root plus the sandbox that enters it.",
    fields = {
        "root": provider_field(Artifact),  # the engine root chroot
        "sandbox": provider_field(Dependency),
    },
)

def engine_cmd(engine: Provider) -> cmd_args:
    """The command prefix entering `engine` (an EngineInfo); the command goes after `--`."""
    return cmd_args(engine.sandbox[RunInfo], "--tools", engine.root, "--bind-cwd", "--source-date-epoch", str(ASSEMBLY_SDE))

def chroot_run(engine: Provider, exe: Dependency, network: bool = False) -> RunInfo:
    """Bind `exe` to `engine` (an EngineInfo) as a RunInfo.

    Pure string assembly — no actions. `exe`'s default output is executed as-is inside
    the chroot (the drivers are python_bootstrap_binary targets whose entry carries a
    shebang; their host RunInfo goes unused). The engine's sandbox only provides the
    exec environment; a driver that needs a target root to install into or run against
    sets it up itself (see rootfs.py)."""
    info = exe[DefaultInfo]
    run = engine_cmd(engine)
    if network:
        run.add("--network")
    run.add("--", cmd_args(info.default_outputs[0], hidden = info.other_outputs))
    return RunInfo(args = run)

def _engine_impl(ctx: AnalysisContext) -> list[Provider]:
    fmt = ctx.attrs.package_format[PackageFormatInfo]
    repositories = ctx.attrs.repositories

    # Action A — select the seed: the lock is a transaction committed by `[resolve]`, so the
    # closures select raw RPMs and their pool-owned payload representations directly without
    # consulting repodata at build time. The source file is read dynamically, so an unlocked
    # `{}` engine analyzes and fails only when demanded.
    packages = download_closure(ctx, ctx.attrs.lock, repositories = repositories)
    payloads = download_closure(
        ctx,
        ctx.attrs.lock,
        name = "extract.closure",
        repositories = repositories,
        representation = "payload",
    )

    # Action B — chroot1: payload-extract the whole seed (no db, no scripts) with
    # the format's bootstrap ur-tool.
    chroot1 = ctx.actions.declare_output("chroot1", dir = True)
    ctx.actions.run(
        cmd_args(fmt.extract[RunInfo], chroot1.as_output(), payloads),
        category = "extract",
    )

    # Action C — chroot2: a *proper* install of the whole seed into a fresh root
    # (db + scriptlets), run with chroot1's tools.
    chroot2 = ctx.actions.declare_output("chroot2", dir = True)
    ctx.actions.run(
        cmd_args(
            chroot_run(
                engine = EngineInfo(root = chroot1, sandbox = ctx.attrs._sandbox),
                exe = fmt.install,
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

    # The refresh half: `[resolve]` re-solves the engine's closure *inside this engine*
    # with the format's plan driver — over the repos' pinned repodata (repos snapshot
    # first, so the solve sees the same refresh's pins; no network needed) — and the
    # orchestrator commits the transaction it writes as the lock fragment.
    resolve = cmd_args(chroot_run(engine = info, exe = fmt.plan), "solve", "--arch", ctx.attrs.arch)
    for repository in repositories:
        repo = repository[RepoInfo]
        resolve.add("--repo", cmd_args(repo.dir, format = repo.id + "={}"))
    for package in ctx.attrs.packages:
        resolve.add("--install", package)
    sub_targets = {
        "resolve": [DefaultInfo(), RunInfo(args = resolve)],
    }

    # The RunInfo makes the engine itself runnable: `buck run <root> -- <cmd>` enters it.
    return [
        DefaultInfo(default_output = chroot2, sub_targets = sub_targets),
        info,
        RunInfo(args = engine_cmd(info)),
    ]

engine = rule(
    impl = _engine_impl,
    attrs = {
        "packages": attrs.list(
            attrs.string(),
            doc = "top-level engine package names (authored; the lock pins the closure)",
        ),
        "repositories": attrs.list(
            attrs.dep(providers = [RepoInfo]),
            doc = "immutable pinned repos `[resolve]` solves against (builds read only the lock)",
        ),
        "package_format": attrs.dep(
            providers = [PackageFormatInfo],
            doc = "the package format plugin (extract/install, plus plan for `[resolve]`)",
        ),
        "lock": attrs.source(
            doc = "the @generated engine transaction (source/repo/pkgid/nevra); seed with `{}`",
        ),
        "arch": attrs.string(default = "x86_64", doc = "the resolution arch"),
        # The one place the sandbox enters the graph: it rides out inside EngineInfo.
        "_sandbox": attrs.exec_dep(default = "tine//distribution:sandbox", providers = [RunInfo]),
    },
)
