# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build reusable execution environments."""

load("//:specs.bzl", "spec_args")
load("//distribution:defs.bzl", "distribution")
load("//package:release.bzl", "OsReleaseInfo")
load(
    "//package:repository.bzl",
    "ConfiguredPackageRepositoryInfo",
    "PackagePoolInfo",
    "PackageRepositoryInfo",
    "box_lock_path",
    "select_package_artifacts",
    "select_repositories",
)
load("//package:solver.bzl", "solve_command", "solver_cache")
load("//package:system.bzl", "PackageSystemInfo")
load("//package:verify.bzl", "repository_verifier")
load("//platforms:architecture.bzl", "architecture")
load(":runtime.bzl", "BoxInfo", "box_run")

_REPOSITORY_PRIORITY = 99

BoxPackagesInfo = provider(
    doc = "The declarations of a box for a child box, or of its lock for one architecture for the child box's lock.",
    fields = {
        # The package system they are named in, which a child box must share.
        "package_system": provider_field(Dependency),
        # The top-level packages, its parent's among them, resolved for the configured architecture.
        "packages": provider_field(list[str]),
    },
)

def _effective_packages(packages: list[str], parent: Dependency | None, package_system: Dependency) -> list[str]:
    """`packages` plus those of `parent`, which must name them in `package_system`."""
    if parent == None:
        return packages
    info = parent[BoxPackagesInfo]
    if info.package_system.label.raw_target() != package_system.label.raw_target():
        fail(
            "box: cannot build on {}: its packages are named for {}, not {}".format(
                parent.label.raw_target(),
                info.package_system.label.raw_target(),
                package_system.label.raw_target(),
            )
        )
    return info.packages + packages

# Layering information for a box that uses this one as a parent.
_BoxRootInfo = provider(
    fields = {
        "repositories": provider_field(list[str]),
        "transaction": provider_field(Artifact),
    },
)

def _repository_labels(repositories: list[Dependency]) -> list[str]:
    return sorted([str(repository.label.raw_target()) for repository in repositories])

def _layer_dir(actions: AnalysisActions, name: str) -> Artifact:
    """Declare a directory output that holds a box root or the overlay work directory for one."""

    # A box root takes several gigabytes. Buck keeps every content-based version of an output until a
    # clean, so each rebuild that changes the root adds another full copy. A rebuild that produces the
    # same root leaves a second full copy at the placeholder path. A plain path holds one copy, which
    # Buck deletes before the action runs again.
    return actions.declare_output(name, dir = True, has_content_based_path = False)

def _configure_repositories(
    ctx: AnalysisContext,
    repositories: list[Dependency],
    verifier_box: BoxInfo | None,
) -> list[ConfiguredPackageRepositoryInfo]:
    """The box's repositories at the native default priority.

    Their packages are verified by `verifier_box`. Use None only for bootstrapping the root box, as
    that has nothing to verify with."""
    configured = []
    keyrings = {}
    for repository in repositories:
        repo = repository[PackageRepositoryInfo]
        rid = repository.label.name
        if repo.dir == None:
            fail("box: repository '{}' has no bootstrap directory".format(rid))
        if repo.baseurl == None:
            fail("box: repository '{}' has no bootstrap base URL".format(rid))
        verifier = None
        if verifier_box != None:
            verifier = repository_verifier(ctx, verifier_box, rid, repo.signing_keys, repo.package_system, repo.dir, keyrings, repo.pinned_at)
        configured.append(
            ConfiguredPackageRepositoryInfo(
                id = rid,
                dependency = repository,
                directory = repo.dir,
                priority = _REPOSITORY_PRIORITY,
                baseurl = repo.baseurl,
                packages = repository[PackagePoolInfo].value,
                verifier = verifier,
            )
        )
    return configured

def _solve(
    ctx: AnalysisContext,
    release: OsReleaseInfo,
    resolver_box: Dependency,
    repositories: list[ConfiguredPackageRepositoryInfo],
    packages: list[str],
    arch: str,
) -> cmd_args:
    """The command resolving `packages` for `arch` in `resolver_box`, against `repositories`."""
    system = release.package_system[PackageSystemInfo]
    solver_caches = [solver_cache(ctx, resolver_box, release.package_system, repository, arch) for repository in repositories] if system.solver_cache else []
    return solve_command(
        ctx = ctx,
        box = resolver_box[BoxInfo],
        system = system,
        repositories = repositories,
        # the macro cannot see into a select(), so do the duplication elimination here
        install = {package: True for package in packages}.keys(),
        arch = arch,
        solver_caches = solver_caches,
    )

def _box_impl(ctx: AnalysisContext) -> list[Provider]:
    release = ctx.attrs.release[OsReleaseInfo]
    system = release.package_system[PackageSystemInfo]
    repositories = select_repositories(
        release.repository_universe,
        ctx.attrs.enable_repository_groups,
        ctx.attrs.disable_repository_groups,
    )
    configured_repositories = _configure_repositories(ctx, repositories, None)
    names = _effective_packages(ctx.attrs.packages, ctx.attrs.parent, release.package_system)

    resolver_box = None
    if ctx.attrs.resolver_box != None:
        resolver_box = ctx.attrs.resolver_box[BoxInfo]

    transaction = ctx.attrs.lock
    if transaction == None:
        if ctx.attrs.resolver_box == None:
            fail("box: a root box has nothing to resolve with and requires a committed lock")
        resolve = _solve(ctx, release, ctx.attrs.resolver_box, configured_repositories, names, ctx.attrs._arch)
        transaction = ctx.actions.declare_output("transaction.json")
        resolve.add("--out", transaction.as_output())
        ctx.actions.run(resolve, category = "box_resolve", allow_cache_upload = True)

    # A predecessor installs the transaction directly. Only a root box must first unpack that
    # same closure into an installer-capable root, without metadata or scriptlets.
    installer_box = resolver_box
    if installer_box == None:
        unverified_packages = select_package_artifacts(
            ctx,
            transaction,
            repositories = configured_repositories,
            suffix = system.package_suffix,
            name = "bootstrap.closure",
        )
        stage1 = _layer_dir(ctx.actions, "stage1")
        ctx.actions.run(
            cmd_args(
                system.extract[RunInfo],
                spec_args(
                    ctx.actions,
                    "extract.spec.json",
                    {
                        "out": stage1.as_output(),
                        "packages": [unverified_packages],
                    },
                ),
            ),
            category = "extract",
        )
        installer_box = BoxInfo(
            layers = [stage1],
            sandbox = ctx.attrs._sandbox,
        )

    # See "Box layers" in docs/design/architecture.md.
    repository_labels = _repository_labels(repositories)
    lower = []
    installed = None
    parent = ctx.attrs.parent
    if parent != None and ctx.attrs.layered and parent[_BoxRootInfo].repositories == repository_labels:
        lower = parent[BoxInfo].layers
        installed = parent[_BoxRootInfo].transaction

    # The predecessor verifies its successor's packages. A root box has only its own stage1 for that,
    # which catches an unsigned or tampered package and a wrong key. But its verify program came out of
    # unverified files: a tampered `rpmkeys` could say "OK" to anything, so stage1 is no root of trust.
    packages = select_package_artifacts(
        ctx,
        transaction,
        repositories = _configure_repositories(ctx, repositories, installer_box),
        suffix = system.package_suffix,
        installed = installed,
    )

    # Use the predecessor or bootstrapped root to install the box, or its layer over the parent.
    stage2 = _layer_dir(ctx.actions, "stage2")
    work = _layer_dir(ctx.actions, "stage2.work") if lower else None
    ctx.actions.run(
        cmd_args(
            box_run(
                box = installer_box,
                exe = system.install,
            ),
            spec_args(
                ctx.actions,
                "install.spec.json",
                {
                    "arch": architecture.spelling(ctx.attrs._arch, system.arch_schema),
                    "box_config": True,
                    "docs": True,
                    "installroot": None,
                    "langs": [],
                    "lower": lower,
                    "packages_dir": packages,
                    "target": stage2.as_output(),
                    "work": work.as_output() if work != None else None,
                },
            ),
        ),
        category = "box",
    )

    info = BoxInfo(
        layers = lower + [stage2],
        sandbox = ctx.attrs._sandbox,
    )
    root = _BoxRootInfo(
        repositories = repository_labels,
        transaction = transaction,
    )
    sub_targets = {
        # Buck prints no path for a target with several default outputs, so a tool that reads a box's
        # layers, such as the editor's ty, builds this list instead. It lists the layers bottom first.
        "layers": [DefaultInfo(default_output = ctx.actions.write_json("layers.json", info.layers), other_outputs = info.layers)],
        "transaction": [DefaultInfo(default_output = transaction)],
    }

    # Running a box target is an interactive act, so its RunInfo is the host-integrated relaxed
    # entry. Build actions never reach it: they take BoxInfo and construct their own hermetic
    # box_run.
    relaxed = box_run(info, relaxed = True, name = ctx.label.name.removesuffix(".box"))
    return [
        DefaultInfo(default_outputs = info.layers, sub_targets = sub_targets),
        info,
        root,
        relaxed,
        BoxPackagesInfo(packages = names, package_system = release.package_system),
    ]

_box = rule(
    impl = _box_impl,
    attrs = {
        "disable_repository_groups": attrs.list(attrs.string(), default = []),
        "enable_repository_groups": attrs.list(attrs.string(), default = []),
        "labels": attrs.list(attrs.string(), default = []),
        "layered": attrs.bool(default = True, doc = "layer over a parent from the same repositories instead of installing on its own"),
        "lock": attrs.option(
            attrs.source(),
            default = None,
            doc = "optional frozen transaction, including remote package transport pins",
        ),
        "packages": attrs.list(
            attrs.string(),
            doc = "top-level box package names used to resolve the effective transaction",
        ),
        "parent": attrs.option(attrs.dep(providers = [BoxInfo, BoxPackagesInfo]), default = None, doc = "box whose packages this one adds to"),
        "release": attrs.dep(providers = [OsReleaseInfo], doc = "base OS release for the box root"),
        "resolver_box": attrs.option(
            attrs.exec_dep(providers = [BoxInfo]),
            default = None,
            doc = "predecessor box used to resolve and install this box's transaction",
        ),
        # The box's incoming transition configures it for the build host, so this is the host's architecture.
        "_arch": attrs.string(default = architecture.configured(), doc = "the resolution arch"),
        # BoxInfo carries this into the rest of the graph.
        "_sandbox": attrs.exec_dep(default = "tine//box:sandbox", providers = [RunInfo]),
    },
    # box.new() sets the incoming transition to the host platform of the release's distribution.
    supports_incoming_transition = True,
)

def _box_lock_impl(ctx: AnalysisContext) -> list[Provider]:
    release = ctx.attrs.release[OsReleaseInfo]
    repositories = _configure_repositories(
        ctx,
        select_repositories(
            release.repository_universe,
            ctx.attrs.enable_repository_groups,
            ctx.attrs.disable_repository_groups,
        ),
        None,
    )
    packages = _effective_packages(ctx.attrs.packages, ctx.attrs.parent, release.package_system)
    return [
        DefaultInfo(),
        RunInfo(args = _solve(ctx, release, ctx.attrs.resolver_box, repositories, packages, ctx.attrs._arch)),
        BoxPackagesInfo(packages = packages, package_system = release.package_system),
    ]

# A box lock resolves the box for one architecture. Its incoming transition to that architecture's
# platform configures the repositories for that architecture, and a `select()` in `packages` picks that
# architecture's entry. A parent box is configured for the host. A child box's lock therefore reads the
# parent's packages from the parent's lock for the same architecture.
_box_lock = rule(
    impl = _box_lock_impl,
    attrs = {
        "disable_repository_groups": attrs.list(attrs.string(), default = []),
        "enable_repository_groups": attrs.list(attrs.string(), default = []),
        "labels": attrs.list(attrs.string(), default = []),
        "packages": attrs.list(attrs.string(), doc = "the box's top-level package names"),
        "parent": attrs.option(attrs.dep(providers = [BoxPackagesInfo]), default = None, doc = "lock of the parent box for the same architecture"),
        "release": attrs.dep(providers = [OsReleaseInfo], doc = "base OS release for the box root"),
        "resolver_box": attrs.exec_dep(providers = [BoxInfo], doc = "box the solver runs in"),
        # Private: the incoming transition decides.
        "_arch": attrs.string(default = architecture.configured(), doc = "the architecture to resolve for"),
    },
    supports_incoming_transition = True,
)

# Every in-box driver runs on the box's own interpreter. Arch names the package `python`, which
# provides this.
_DEFAULT_PACKAGES = ["python3"]

def new(
    name: str,
    packages: list[str] | Select,
    release: str,
    architectures: list[str] = [],
    disable_repository_groups: list[str] | None = None,
    enable_repository_groups: list[str] | None = None,
    parent: str | None = None,
    resolver_box: str | None = None,
    distro: str | None = None,
    root: bool = False,
    labels: list[str] = [],
    visibility: list[str] | None = None,
    **kwargs,
) -> None:
    """Declare a box rooted in one base OS release.

    A release names its own box beside it, so an unset `resolver_box` resolves through that sibling.
    A `root` box has none: it bootstraps by extracting its transaction, which is how the box a
    release names is built in the first place.

    A box with a `parent` adds its own `packages` on top of that box's. Both must use the same
    package system. When both are installed from the same repositories, the box is a layer over
    its parent unless `layered = False`; see "Box layers" in docs/design/architecture.md.

    `architectures` are the ones this box keeps a committed lock for: it is built for the host's
    alone, but refresh-catalog writes a lock per architecture, so a build on any of them has one to
    start from. A box that names none resolves during the build instead, which a root box cannot do.
    A box with a `parent` can only keep a lock for an architecture that its parent keeps one for.

    The box is configured for the host and the distribution of its release, so a
    `distribution.select()` in `packages` picks the entry of the release, or else of its family.
    `distro` names the distribution target, by default the one beside `release`.
    """
    if not name.endswith(".box"):
        fail("box name must end with '.box': {}".format(name))
    if root and resolver_box != None:
        fail("box: a root box bootstraps itself and takes no resolver_box: {}".format(name))
    if root and parent != None:
        fail("box: a root box bootstraps itself and takes no parent: {}".format(name))
    if not root and resolver_box == None:
        if not release.endswith(".release"):
            fail("box: cannot derive a resolver box from release '{}'; pass resolver_box".format(release))
        resolver_box = release.removesuffix(".release") + ".box"
    if distro == None:
        if not release.endswith(".release"):
            fail("box: cannot derive a distribution from release '{}'; pass distro".format(release))
        distro = release.removesuffix(".release") + ".distribution"
    stem = name.removesuffix(".box")
    packages = _DEFAULT_PACKAGES + packages

    # The lock of each architecture, None until refresh-catalog has written one. A box that keeps
    # none has nothing to select over and resolves during the build instead.
    locks = {}
    for arch in architectures:
        committed = glob([box_lock_path(stem, arch)])
        locks[arch] = committed[0] if committed else None

    _box(
        name = name,
        packages = packages,
        release = release,
        resolver_box = resolver_box,
        disable_repository_groups = disable_repository_groups,
        enable_repository_groups = enable_repository_groups,
        parent = parent,
        lock = architecture.select(locks) if locks else None,
        labels = labels,
        incoming_transition = distro + ".host",
        # A box is built for the host. Without a lock for the host's architecture, it cannot be built.
        target_compatible_with = distribution.serves(name, architectures) if architectures else [],
        visibility = visibility,
        **kwargs,
    )

    # A lock is resolved in its resolver box on the host. A root box is its own resolver, so its locks
    # fail to build on a host that it keeps no lock for. A lock is configured for the architecture it
    # resolves for, so this check reads the host's architecture when the package loads.
    lock_compatible_with = []
    if root and architecture.host() not in architectures:
        lock_compatible_with = select({"DEFAULT": select_incompatible("{} keeps no lock for this host".format(name))})

    # A root box resolves through itself: its committed lock is what builds the root that then says
    # what the lock should be.
    for arch in architectures:
        _box_lock(
            # Keep in sync with BOX_LOCK_INFIX in tools/catalog.py.
            name = "{}.lock.{}".format(name, arch),
            packages = packages,
            release = release,
            resolver_box = ":" + name if root else resolver_box,
            disable_repository_groups = disable_repository_groups,
            enable_repository_groups = enable_repository_groups,
            parent = "{}.lock.{}".format(parent, arch) if parent != None else None,
            incoming_transition = "{}.{}".format(distro, arch),
            labels = ["tine:box-lock"],
            target_compatible_with = lock_compatible_with,
            # The lock of a child box in another package depends on this lock. Without the box's
            # visibility, that lock fails to configure.
            visibility = visibility,
        )
