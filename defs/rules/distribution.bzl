"""Distribution rules: the format-neutral machinery //distribution composes."""

load(":engine.bzl", "EngineInfo", "chroot_run")
load(":package_format.bzl", "PackageFormatInfo")
load(":repo.bzl", "RepoInfo", "download_closure")

# Prefer extra-packages (our own builds) over the upstream repos even when
# upstream carries a newer version that we didn't import/merge yet.
_EXTRA_REPO_PRIORITY = 50

DistributionInfo = provider(
    # An engine + format plugin + buildroot repos and base packages, plus each repo's
    # prebuilt solver cache. The engine root may belong to another distribution (CentOS
    # builds in the Fedora engine root).
    doc = "A distribution build target.",
    fields = {
        "engine": provider_field(EngineInfo),
        # typing.Any, not PackageFormatInfo: provider_field rejects a provider instance under its
        # own type, and reads come back as the generic Provider anyway.
        "package_format": provider_field(typing.Any),
        "buildroot_repositories": provider_field(list[RepoInfo]),
        "buildroot_base_packages": provider_field(list[str]),
        "solv_caches": provider_field(dict[str, Artifact]),  # repo id -> prebuilt cache dir (plan make-cache)
    },
)

def _distribution_impl(ctx: AnalysisContext) -> list[Provider]:
    engine = ctx.attrs.engine[EngineInfo]
    fmt = ctx.attrs.package_format[PackageFormatInfo]
    repos = [r[RepoInfo] for r in ctx.attrs.buildroot_repositories]

    # Loading a repo means parsing its metadata XML into libdnf5's .solv cache —
    # re-paid by every plan action, since the sandbox cachedir is ephemeral (for
    # Fedora that's ~1GB of XML, filelists being 3/4 of it). Prebuild each repo's
    # cache once (plan make-cache, in the engine root) and let every plan seed
    # from it. Declared on the distribution so all its consumers share one cache
    # build per repo; the actions run only when a plan actually demands them.
    caches = {}
    for repo in repos:
        cache = ctx.actions.declare_output("solv-cache-{}".format(repo.id), dir = True)
        ctx.actions.run(
            cmd_args(
                chroot_run(engine = engine, exe = fmt.plan),
                "make-cache",
                "--repo",
                cmd_args(repo.dir, format = repo.id + "={}"),
                "--out",
                cache.as_output(),
            ),
            category = "solvcache",
            identifier = repo.id,
        )
        caches[repo.id] = cache

    return [
        DefaultInfo(),
        DistributionInfo(
            engine = engine,
            package_format = fmt,
            buildroot_repositories = repos,
            buildroot_base_packages = ctx.attrs.buildroot_base_packages,
            solv_caches = caches,
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

def _pin_repodata_impl(actions, id: str, lock: ArtifactValue, repo: OutputArtifact) -> list[Provider]:
    # Assemble a `repodata/` tree as a local `file://` repo: the lock's filtered repomd
    # plus the pinned stream files it references, downloaded right here. No packages:
    # the resolved subset is downloaded at build time (Action 2) from `baseurl`.
    data = lock.read_json()
    if not data:
        fail("repository '{}' is not locked yet (its fragment is `{{}}`); run refresh-catalog".format(id))
    repomd = actions.write("repomd.xml", data["repomd"])
    tree = {"repodata/repomd.xml": repomd}
    for f in data["streams"]:
        out = actions.declare_output(f["out"])
        actions.download_file(out, f["url"], sha256 = f["sha256"], size_bytes = f["size"])
        tree["repodata/" + f["out"]] = out
    actions.copied_dir(repo, tree)
    return []

_pin_repodata = dynamic_actions(
    impl = _pin_repodata_impl,
    attrs = {
        "id": dynattrs.value(str),
        "lock": dynattrs.artifact_value(),
        "repo": dynattrs.output(),
    },
)

def _remote_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    # The refresh half, as sub-targets: `[manifest]` is the authored data as JSON (the
    # snapshot driver's input, also handed to distribution resolvers), `[snapshot]`
    # runs the driver to (re)pin the repodata (host — pinning is pure fetch-and-filter).
    rid = ctx.label.name
    fmt = ctx.attrs.package_format[PackageFormatInfo]
    manifest = ctx.actions.write("manifest.json", json.encode({"baseurl": ctx.attrs.baseurl, "id": rid}))
    sub_targets = {
        "manifest": [DefaultInfo(default_output = manifest)],
        "snapshot": [DefaultInfo(), RunInfo(args = cmd_args(fmt.snapshot[RunInfo], "--manifest", manifest))],
    }

    # The pinned half. The lock is a source file read at action time (a dynamic action —
    # analysis can't read file contents), so an unlocked repo (fragment seeded `{}`)
    # analyzes fine and fails only when its repodata is actually demanded.
    repo_dir = ctx.actions.declare_output("repo", dir = True)
    ctx.actions.dynamic_output_new(_pin_repodata(id = rid, lock = ctx.attrs.lock, repo = repo_dir.as_output()))
    return [
        DefaultInfo(default_output = repo_dir, sub_targets = sub_targets),
        RepoInfo(id = rid, dir = repo_dir, packages = [], manifest = manifest, baseurl = ctx.attrs.baseurl),
    ]

# A remote repository: a pinned snapshot of a real distro repodata, which in turn pins the
# distro rpms. A first-class catalog citizen shared across distributions — the target
# name doubles as the repo id. The plan resolves against the metadata; the resolved rpms
# are fetched lazily at build time from `baseurl`. Declared via a format wrapper
# (rpm_remote_repository) that binds `package_format` and defaults `lock`.
remote_repository = rule(
    impl = _remote_repository_impl,
    attrs = {
        "baseurl": attrs.string(doc = "the real remote baseurl (for download URLs)"),
        "package_format": attrs.dep(providers = [PackageFormatInfo], doc = "the package format plugin (supplies the snapshot driver)"),
        "lock": attrs.source(
            doc = "the @generated snapshot fragment, {repomd: <filtered xml>, streams: " +
                  "[{out, url, sha256, size}]} (size skips the HEAD probe) — the schema is " +
                  "documented here because the JSON fragments can't carry comments; seed with `{}`",
        ),
    },
)

_ExtraRepoInfo = provider(
    doc = "The extra-packages repository tree, carried out of the anon target for its promise.",
    fields = {"repo": provider_field(Artifact)},
)

def _extra_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    """createrepo our own build outputs (rpms dirs) into a local repository.

    Each rpm's location_href is its rpms-dir index + basename, so a consumer can map a
    resolved file:// URL back to the originating input dir (see repo.bzl's download_closure)."""
    distribution = ctx.attrs.distribution[DistributionInfo]
    repo = ctx.actions.declare_output("repo", dir = True)
    createrepo = cmd_args(
        chroot_run(engine = distribution.engine, exe = distribution.package_format.createrepo),
        "--out",
        repo.as_output(),
    )
    for rpms_dir in ctx.attrs.packages:
        createrepo.add("--packages-dir", rpms_dir)
    ctx.actions.run(createrepo, category = "createrepo")
    return [DefaultInfo(default_output = repo), _ExtraRepoInfo(repo = repo)]

# The repo is pure data — (distribution, packages) fully determine it — so it's an anon
# target: roots with different install sets but the same extra packages share one
# repodata build, same as the install root itself is shared (see _install_packages).
_extra_repository = anon_rule(
    impl = _extra_repository_impl,
    attrs = {
        "distribution": attrs.dep(providers = [DistributionInfo]),
        "packages": attrs.list(attrs.source()),
    },
    artifact_promise_mappings = {
        "repo": lambda p: p[_ExtraRepoInfo].repo,
    },
)

_RootInfo = provider(
    doc = "The installed root tree, carried out of the anon target for its `root` promise.",
    fields = {"root": provider_field(Artifact)},
)

def _install_actions(
        ctx: AnalysisContext,
        distribution: Dependency,
        install: list[str],
        stack: list[Artifact],
        extra_packages: list[Artifact]) -> Artifact:
    """The plan → download → install action triple; returns the fresh root or the delta.

    Format-neutral: drives the distribution's own plan + install drivers, with buck's native
    download_file in between. `extra_packages` (our own builds) are createrepo'd into a priority
    extra repo (Action 0) so the resolve prefers them over upstream. With a `stack` (an existing
    tree as overlay deltas, bottom..top), the plan resolves against the merged installed set
    too — only the increment is fetched — and the install lands as a new delta over it."""
    info = distribution[DistributionInfo]
    repos = info.buildroot_repositories
    engine = info.engine
    fmt = info.package_format

    # Action 0 (self-hosting only) — createrepo our own build outputs (the locked
    # buildroot_deps' rpms dirs) into a local extra-packages repo, via a nested anon target so
    # roots with different install sets but identical extra packages share it. Its lower
    # priority number outranks the upstream repos, so the plan takes our build for any cap we
    # provide — even when upstream carries a newer NEVRA we didn't import yet.
    extra_repo = None
    if extra_packages:
        extra_repo = ctx.actions.anon_target(_extra_repository, {
            "name": "//extra-repository:{}".format(distribution.label.name),
            "distribution": distribution,
            "packages": extra_packages,
        }).artifact("repo")
        extra_repo = ctx.actions.assert_short_path(extra_repo, short_path = "repo")

    # Action 1 — plan: resolve the closure over the repos' repodata → a transaction.
    # Re-runs on any repodata change, but its output is stable unless *this* closure changed.
    tx = ctx.actions.declare_output("transaction.json")
    plan = cmd_args(chroot_run(engine = engine, exe = fmt.plan), "solve", "--out", tx.as_output())
    if extra_repo != None:
        # Our builds resolve as a local file:// repo; download (Action 2) reads the rpms in place.
        plan.add("--repo", cmd_args(extra_repo, format = "extra={}"))
        plan.add("--baseurl", cmd_args(extra_repo, format = "extra=file://{}"))
        plan.add("--priority", "extra={}".format(_EXTRA_REPO_PRIORITY))
    for repository in repos:
        plan.add("--repo", cmd_args(repository.dir, format = repository.id + "={}"))
        plan.add("--baseurl", "{}={}".format(repository.id, repository.baseurl))
        plan.add("--priority", "{}={}".format(repository.id, repository.priority))
        plan.add("--cache", info.solv_caches[repository.id])
    for lower in stack:
        plan.add("--lower", lower)
    for cap in install:
        plan.add("--install", cap)
    ctx.actions.run(plan, category = "plan")

    # Action 2 — download the resolved transaction (extra-packages rpms are projected in
    # place from their rpms dirs instead of fetched).
    closure = download_closure(ctx, tx, extra_packages = extra_packages)

    # Action 3 — install that closure (it's already the exact set). Cuts off when the
    # closure is unchanged.
    out = ctx.actions.declare_output("install.delta" if stack else "root", dir = True)
    cmd = cmd_args(
        chroot_run(engine = engine, exe = fmt.install),
        "--packages-dir",
        closure,
        "--target",
        out.as_output(),
    )
    if stack:
        # overlayfs leaves the workdir dirty; declared so it lands in buck-out (on the
        # upper's filesystem) and buck tracks it.
        cmd.add("--work", ctx.actions.declare_output("install.work", dir = True).as_output())
    for lower in stack:
        cmd.add("--lower", lower)
    ctx.actions.run(cmd, category = "assemble")
    return out

def _install_packages_impl(ctx: AnalysisContext) -> list[Provider]:
    root = _install_actions(ctx, ctx.attrs.distribution, ctx.attrs.install, [], ctx.attrs.extra_packages)
    return [DefaultInfo(default_output = root), _RootInfo(root = root)]

# A fresh install root (an rpm buildroot, an image base layer) is pure data —
# (distribution, install set, extra packages) fully determine it — so it's an anon target: two
# consumers with the same distribution and install set share one analysis (and thus one build),
# not just an action-cache hit. The sandbox rides in on the distribution's EngineInfo,
# sidestepping anon rules' inability to declare exec_dep.
_install_packages = anon_rule(
    impl = _install_packages_impl,
    attrs = {
        "distribution": attrs.dep(providers = [DistributionInfo]),
        "install": attrs.list(attrs.string()),
        "extra_packages": attrs.list(attrs.source(), default = []),
    },
    artifact_promise_mappings = {
        "root": lambda p: p[_RootInfo].root,
    },
)

def install_packages(
        ctx: AnalysisContext,
        distribution: Dependency,
        install: list[str],
        stack: list[Artifact] = [],
        extra_packages: list[Artifact] = []) -> Artifact:
    """Plan + install `install`, returning a fresh full root (empty `stack`) or the increment's delta.

    A fresh root goes through the shared `_install_packages` anon target (see there): `install`
    is sorted so equal sets share, and `assert_short_path` pins the promise's short path (the anon
    output's own — a shared target can't take a per-caller name) so it's usable before the anon
    target is analyzed. `extra_packages` (the rpms dirs of our own builds that outrank the upstream
    repos, empty for a pure-seed root) keys the anon target too, so distinct sets don't collide but
    identical triples still share. An incremental install is keyed on the caller's own `stack`, so
    there's nothing to share — the actions run inline."""
    if not stack:
        root = ctx.actions.anon_target(_install_packages, {
            # A friendlier log label than the default `anon//:_install_packages@<hash>`. Derived
            # purely from the distribution (already a key attr), so it doesn't split sharing;
            # the `@<hash>` buck appends still distinguishes distinct install sets.
            "name": "//install-packages:{}".format(distribution.label.name),
            "distribution": distribution,
            "install": sorted(install),
            "extra_packages": extra_packages,
        }).artifact("root")
        return ctx.actions.assert_short_path(root, short_path = "root")
    return _install_actions(ctx, distribution, install, stack, extra_packages)
