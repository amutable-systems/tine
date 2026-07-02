"""Distribution rules: the format-neutral machinery //distribution composes."""

load(":engine.bzl", "EngineInfo", "chroot_run")
load(":package_format.bzl", "PackageFormatInfo")

RepoInfo = provider(
    # `dir` holds the `repodata/` the plan resolves against. Two flavors:
    #   - remote: `dir` is just pinned repodata (no packages); `baseurl` is the real remote base,
    #     so plan turns each resolved package's location into a download URL. `packages` is empty —
    #     the buildroot's resolved subset is fetched lazily at build time.
    #   - local: `dir` is a createrepo'd tree with the rpms present; `packages` are those artifacts
    #     (keyed by basename to scope an install); `baseurl` is "".
    doc = "A package repository.",
    fields = {
        "id": provider_field(str),
        "dir": provider_field(Artifact),
        "packages": provider_field(list[Artifact]),
        "baseurl": provider_field(str, default = ""),
    },
)

DistributionInfo = provider(
    # Pure data (an engine + format plugin + buildroot repos and base packages). The engine
    # root may belong to another distribution (CentOS builds in the Fedora engine root).
    doc = "A distribution build target.",
    fields = {
        "engine": provider_field(EngineInfo),
        # typing.Any, not PackageFormatInfo: provider_field rejects a provider instance under its
        # own type, and reads come back as the generic Provider anyway.
        "package_format": provider_field(typing.Any),
        "buildroot_repositories": provider_field(list[RepoInfo]),
        "buildroot_base_packages": provider_field(list[str]),
    },
)

def _distribution_impl(ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(),
        DistributionInfo(
            engine = ctx.attrs.engine[EngineInfo],
            package_format = ctx.attrs.package_format[PackageFormatInfo],
            buildroot_repositories = [r[RepoInfo] for r in ctx.attrs.buildroot_repositories],
            buildroot_base_packages = ctx.attrs.buildroot_base_packages,
        ),
    ]

distribution = rule(
    impl = _distribution_impl,
    attrs = {
        "engine": attrs.dep(providers = [EngineInfo], doc = "the engine whose root the drivers run in"),
        "package_format": attrs.dep(providers = [PackageFormatInfo], doc = "the package format plugin supplying the build drivers"),
        "buildroot_repositories": attrs.list(attrs.dep(providers = [RepoInfo]), doc = "repos holding this distribution's buildroot packages"),
        "buildroot_base_packages": attrs.list(attrs.string(), doc = "always-installed base package names"),
    },
)

def _local_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    # Bind the format's createrepo driver to run in the engine root (the repodata
    # writer lives there); chroot_run runs it at the fixed assembly epoch so the
    # repodata is reproducible.
    fmt = ctx.attrs.package_format[PackageFormatInfo]
    createrepo = chroot_run(engine = ctx.attrs.engine[EngineInfo], exe = fmt.createrepo)
    packages = [p[DefaultInfo].default_outputs[0] for p in ctx.attrs.packages]

    # Generate the repository (packages + repodata/) so libdnf5 can resolve against
    # it as a real local repo.
    repo_dir = ctx.actions.declare_output("repo", dir = True)
    cmd = cmd_args(createrepo, "--out", repo_dir.as_output())
    for p in packages:
        cmd.add("--package", p)
    ctx.actions.run(cmd, category = "createrepo")

    return [
        DefaultInfo(default_output = repo_dir),
        RepoInfo(id = ctx.attrs.id, dir = repo_dir, packages = packages),
    ]

# A local repository: generate repodata (in an engine root) from a list of package targets
# (the rpms we build ourselves) into a real local repo addressed by `id`, also exposing the
# per-package artifacts. This is how our own builds are served to a buildroot resolve.
local_repository = rule(
    impl = _local_repository_impl,
    attrs = {
        "id": attrs.string(doc = "the repo id"),
        "packages": attrs.list(attrs.dep(), doc = "the rpms this repo publishes (one dep each)"),
        "engine": attrs.dep(providers = [EngineInfo], doc = "the engine whose root the createrepo driver runs in"),
        "package_format": attrs.dep(providers = [PackageFormatInfo], doc = "the package format plugin supplying the createrepo driver"),
    },
)

def _remote_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    # Assemble a `repodata/` tree as a local `file://` repo: repomd (pinned in catalog)
    # plus the pinned stream files it references. No packages here: the buildroot's
    # resolved subset is downloaded at build time (Action 2) from `baseurl`.
    repomd = ctx.actions.write("repomd.xml", ctx.attrs.repomd)
    streams = [f[DefaultInfo].default_outputs[0] for f in ctx.attrs.streams]
    tree = {"repodata/repomd.xml": repomd}
    tree.update({"repodata/" + f.basename: f for f in streams})
    repo_dir = ctx.actions.copied_dir("repo", tree)
    return [
        DefaultInfo(default_output = repo_dir),
        RepoInfo(id = ctx.attrs.id, dir = repo_dir, packages = [], baseurl = ctx.attrs.baseurl),
    ]

# A remote repository: a pinned snapshot of a real distro repodata, which in turn pins the
# distro rpms. The plan resolves against the metadata; the resolved rpms are fetched
# lazily at build time from `baseurl`.
remote_repository = rule(
    impl = _remote_repository_impl,
    attrs = {
        "id": attrs.string(doc = "the repo id"),
        "baseurl": attrs.string(doc = "the real remote baseurl (for download URLs)"),
        "repomd": attrs.string(doc = "the filtered repomd.xml content (lists only the pinned streams)"),
        "streams": attrs.list(attrs.dep(), doc = "pinned metadata stream http_files (primary + filelists)"),
    },
)

def _download_closure(actions: AnalysisActions, tx: ArtifactValue, closure: OutputArtifact) -> list[Provider]:
    # Dynamic: the resolved set is only known once `plan` has run. Fetch rpms into
    # content-based output for global sharing. The buildroot closure is a symlink tree.
    rpms = {}
    for entry in tx.read_json():
        name = entry["url"].rsplit("/", 1)[-1]
        out = actions.declare_output("rpm", name, has_content_based_path = True)
        actions.download_file(out, entry["url"], sha256 = entry["sha256"])
        rpms[name] = out
    actions.symlinked_dir(closure, rpms)
    return []

_download = dynamic_actions(
    impl = _download_closure,
    attrs = {
        "tx": dynattrs.artifact_value(),
        "closure": dynattrs.output(),
    },
)

_BuildrootInfo = provider(
    doc = "The assembled buildroot tree, carried out of the anon target for its `root` promise.",
    fields = {"root": provider_field(Artifact)},
)

def _buildroot_impl(ctx: AnalysisContext) -> list[Provider]:
    """Resolve `install` over the distribution's repos and install the closure into a fresh root.

    Format-neutral: it drives the distribution's own plan + install drivers, with buck's native
    download_file in between.

    Three actions: (1) plan resolves the install set's runtime closure over the repos'
    pinned repodata → a transaction ([{url, sha256}]); (2) a dynamic download_file per rpm
    fetches exactly that subset (sha-verified, content-addressed → shared across buildroots);
    (3) install lays that closure into a fresh root.
    """
    distribution = ctx.attrs.distribution[DistributionInfo]
    repos = distribution.buildroot_repositories
    fmt = distribution.package_format
    engine = distribution.engine

    # Action 1 — plan: resolve the closure over the repos' repodata → a transaction.
    # Re-runs on any repodata change, but its output is stable unless *this* closure changed.
    tx = ctx.actions.declare_output("transaction.json")
    plan = cmd_args(
        chroot_run(engine = engine, exe = fmt.plan),
        "--out",
        tx.as_output(),
    )
    for repository in repos:
        plan.add("--repo", cmd_args(repository.dir, format = repository.id + "={}"))
        plan.add("--baseurl", "{}={}".format(repository.id, repository.baseurl))
    for cap in ctx.attrs.install:
        plan.add("--install", cap)
    ctx.actions.run(plan, category = "plan")

    # Action 2 — download the resolved transaction. lazy (only this buildroot's subset),
    # shared (downloaded once and re-used via symlink trees), dynamic (resolved set isn't
    # known until `plan` runs).
    closure = ctx.actions.declare_output("buildroot.closure", dir = True)
    ctx.actions.dynamic_output_new(_download(tx = tx, closure = closure.as_output()))

    # Action 3 — install that closure into a fresh root (it's already the exact set).
    # Cuts off when the closure is unchanged.
    root = ctx.actions.declare_output("root", dir = True)
    ctx.actions.run(
        cmd_args(
            chroot_run(engine = engine, exe = fmt.install),
            "--packages-dir",
            closure,
            "--target",
            root.as_output(),
        ),
        category = "assemble",
    )
    return [DefaultInfo(default_output = root), _BuildrootInfo(root = root)]

# A buildroot is pure data — (distribution, install set) fully determine it — so it's an
# anon target: two consumers with the same distribution and install set share one
# analysis (and thus one build), not just an action-cache hit. The sandbox rides in on
# the distribution's EngineInfo, sidestepping anon rules' inability to declare exec_dep.
_buildroot = anon_rule(
    impl = _buildroot_impl,
    attrs = {
        "distribution": attrs.dep(providers = [DistributionInfo]),
        "install": attrs.list(attrs.string()),
    },
    artifact_promise_mappings = {
        "root": lambda p: p[_BuildrootInfo].root,
    },
)

def assemble_root(ctx: AnalysisContext, distribution: Dependency, install: list[str]) -> Artifact:
    """Assemble a buildroot via the shared `_buildroot` anon target, returning its tree.

    `distribution` is the dep (not its DistributionInfo) so the anon target keys on it;
    `install` is sorted so equal sets share regardless of the order the caller assembled
    base + BuildRequires in. The tree's short path is the anon output's own (`root`) — a
    shared target can't take a per-caller name; `assert_short_path` just pins it so the
    promise is usable before the anon target is analyzed.
    """
    root = ctx.actions.anon_target(_buildroot, {
        # A friendlier log label than the default `anon//:_buildroot@<hash>`. Derived
        # purely from the distribution (already a key attr), so it doesn't split sharing;
        # the `@<hash>` buck appends still distinguishes distinct install sets.
        "name": "//buildroot:{}".format(distribution.label.name),
        "distribution": distribution,
        "install": sorted(install),
    }).artifact("root")
    return ctx.actions.assert_short_path(root, short_path = "root")
