# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build reusable execution environments."""

load("//:specs.bzl", "spec_args")
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
            verifier = repository_verifier(
                ctx,
                verifier_box,
                rid,
                repo.signing_keys,
                repo.package_system,
                repo.dir,
                keyrings,
                repo.pinned_at,
                repo.verify_spec,
            )
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

    resolver_box = None
    if ctx.attrs.resolver_box != None:
        resolver_box = ctx.attrs.resolver_box[BoxInfo]

    transaction = ctx.attrs.lock
    if transaction == None:
        if ctx.attrs.resolver_box == None:
            fail("box: a root box has nothing to resolve with and requires a committed lock")
        resolve = _solve(ctx, release, ctx.attrs.resolver_box, configured_repositories, ctx.attrs.packages, ctx.attrs._arch)
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
        stage1 = ctx.actions.declare_output("stage1", dir = True)
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
            root = stage1,
            sandbox = ctx.attrs._sandbox,
        )

    # The predecessor verifies its successor's packages. A root box has only its own stage1 for that,
    # which catches an unsigned or tampered package and a wrong key. But its verify program came out of
    # unverified files: a tampered `rpmkeys` could say "OK" to anything, so stage1 is no root of trust.
    packages = select_package_artifacts(
        ctx,
        transaction,
        repositories = _configure_repositories(ctx, repositories, installer_box),
        suffix = system.package_suffix,
    )

    # Use the predecessor or bootstrapped root to produce the fully installed box.
    stage2 = ctx.actions.declare_output("stage2", dir = True)
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
                    "lower": [],
                    "packages_dir": packages,
                    "target": stage2.as_output(),
                    "work": None,
                },
            ),
        ),
        category = "box",
    )

    info = BoxInfo(
        root = stage2,
        sandbox = ctx.attrs._sandbox,
    )
    sub_targets = {"transaction": [DefaultInfo(default_output = transaction)]}

    return [DefaultInfo(default_output = stage2, sub_targets = sub_targets), info]

_box = rule(
    impl = _box_impl,
    attrs = {
        "disable_repository_groups": attrs.list(attrs.string(), default = []),
        "enable_repository_groups": attrs.list(attrs.string(), default = []),
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
            attrs.exec_dep(providers = [BoxInfo]),
            default = None,
            doc = "predecessor box used to resolve and install this box's transaction",
        ),
        # Private: A box is only ever reached as an exec_dep, so that is the build host's architecture.
        "_arch": attrs.string(default = architecture.configured(), doc = "the resolution arch"),
        # BoxInfo carries this into the rest of the graph.
        "_sandbox": attrs.exec_dep(default = "tine//box:sandbox", providers = [RunInfo]),
    },
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
    return [
        DefaultInfo(),
        RunInfo(args = _solve(ctx, release, ctx.attrs.resolver_box, repositories, ctx.attrs.packages, ctx.attrs._arch)),
    ]

# What a box is made of, resolved for one architecture. The repositories come in under the target
# configuration, so an incoming transition to that architecture's platform is what makes them serve
# its metadata; the box running the solver stays an exec_dep, and so the host's.
_box_lock = rule(
    impl = _box_lock_impl,
    attrs = {
        "disable_repository_groups": attrs.list(attrs.string(), default = []),
        "enable_repository_groups": attrs.list(attrs.string(), default = []),
        "labels": attrs.list(attrs.string(), default = []),
        "packages": attrs.list(attrs.string(), doc = "the box's top-level package names"),
        "release": attrs.dep(providers = [OsReleaseInfo], doc = "base OS release for the box root"),
        "resolver_box": attrs.exec_dep(providers = [BoxInfo], doc = "box the solver runs in"),
        # Private: the incoming transition decides.
        "_arch": attrs.string(default = architecture.configured(), doc = "the architecture to resolve for"),
    },
    supports_incoming_transition = True,
)

def _box_alias_impl(ctx: AnalysisContext) -> list[Provider]:
    # Running a box target is an interactive act, so its RunInfo is the host-integrated relaxed
    # entry. Build actions never reach it: they take BoxInfo and construct their own hermetic
    # box_run.
    relaxed = box_run(ctx.attrs.actual[BoxInfo], relaxed = True, name = ctx.label.name.removesuffix(".box"))
    return ctx.attrs.actual.providers + [relaxed]

# The box's public name. An indirection to build the box only once, regardless of the caller's target
# configuration; see "Box bootstrap" in docs/design/architecture.md.
_box_alias = rule(
    impl = _box_alias_impl,
    attrs = {
        "actual": attrs.exec_dep(providers = [BoxInfo]),
        "labels": attrs.list(attrs.string(), default = []),
    },
)

# Avoid rebuilding boxes for different caller configurations (they are independent of that), only build
# them for different execution platforms (we just have one).
_EXECUTION_CONFIGURATION = "prelude//cfg/exec_platform/marker:is_exec_platform[true]"

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
    resolver_box: str | None = None,
    root: bool = False,
    labels: list[str] = [],
    visibility: list[str] | None = None,
    **kwargs,
) -> None:
    """Declare a box rooted in one base OS release.

    A release names its own box beside it, so an unset `resolver_box` resolves through that sibling.
    A `root` box has none: it bootstraps by extracting its transaction, which is how the box a
    release names is built in the first place.

    `architectures` are the ones this box keeps a committed lock for: it is built for the host's
    alone, but refresh-catalog writes a lock per architecture, so a build on any of them has one to
    start from. A box that names none resolves during the build instead, which a root box cannot do.
    """
    if not name.endswith(".box"):
        fail("box name must end with '.box': {}".format(name))
    if root and resolver_box != None:
        fail("box: a root box bootstraps itself and takes no resolver_box: {}".format(name))
    if not root and resolver_box == None:
        if not release.endswith(".release"):
            fail("box: cannot derive a resolver box from release '{}'; pass resolver_box".format(release))
        resolver_box = release.removesuffix(".release") + ".box"
    stem = name.removesuffix(".box")
    packages = _DEFAULT_PACKAGES + packages

    # The lock of each architecture, None until refresh-catalog has written one. A box that keeps
    # none has nothing to select over and resolves during the build instead.
    locks = {}
    for arch in architectures:
        committed = glob([box_lock_path(stem, arch)])
        locks[arch] = committed[0] if committed else None

    _box(
        name = name + ".exec",
        packages = packages,
        release = release,
        resolver_box = resolver_box,
        disable_repository_groups = disable_repository_groups,
        enable_repository_groups = enable_repository_groups,
        lock = architecture.select(locks) if locks else None,
        target_compatible_with = [_EXECUTION_CONFIGURATION],
        **kwargs,
    )
    _box_alias(
        name = name,
        actual = ":" + name + ".exec",
        labels = labels,
        visibility = visibility,
    )

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
            incoming_transition = "tine//platforms:" + arch,
            labels = ["tine:box-lock"],
        )
