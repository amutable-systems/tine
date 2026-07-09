"""rpm format defs: rpm_package (build one source rpm into its binary rpms) plus the
rpm-bound catalog wrappers (rpm_remote_repository / rpm_engine / rpm_distribution).

The wrappers preconfigure their format-neutral rule with the rpm plugin
(`@tine//distribution/rpm:package_format`) and default each lock to the `<name>.json`
fragment beside the caller's BUCK (seed it with `{}`; `buck run tine//tools:refresh-catalog` fills it).
A catalog BUCK composes them package-relative: a distribution names its engine and
repository sibling targets.
"""

load(":distribution.bzl", "DistributionInfo", "distribution", "install_packages", "remote_repository")
load(":engine.bzl", "chroot_run", engine_rule = "engine")  # aliased: `engine` is an arg below

_RPM_PACKAGE_FORMAT = "@tine//distribution/rpm:package_format"

def rpm_remote_repository(name: str, baseurl: str, lock: str | None = None, **kwargs) -> None:
    """A remote_repository preconfigured for rpm (see the module docstring)."""
    remote_repository(
        name = name,
        baseurl = baseurl,
        lock = lock or (name + ".json"),
        package_format = _RPM_PACKAGE_FORMAT,
        **kwargs
    )

def rpm_engine(name: str, packages: list[str], repositories: list[str], lock: str | None = None, **kwargs) -> None:
    """An engine preconfigured for rpm (see the module docstring)."""
    engine_rule(
        name = name,
        packages = packages,
        repositories = repositories,
        lock = lock or (name + ".json"),
        package_format = _RPM_PACKAGE_FORMAT,
        **kwargs
    )

def rpm_distribution(name: str, engine: str, repositories: list[str], buildroot: list[str], **kwargs) -> None:
    """A distribution preconfigured for rpm (see the module docstring).

    Nothing is derived per distribution — no lock: its engine and repositories carry
    the pins."""
    distribution(
        name = name,
        engine = engine,
        package_format = _RPM_PACKAGE_FORMAT,
        buildroot_repositories = repositories,
        buildroot_base_packages = buildroot,
        **kwargs
    )

def _rpm_package_impl(ctx: AnalysisContext) -> list[Provider]:
    distribution = ctx.attrs.distribution[DistributionInfo]

    # The buildroot is an overlay stack. The distribution's base packages go into a shared
    # lowerdir — identical for every package, so all of a distribution's builds collapse onto one
    # base install — and this package's BuildRequires layer on top as a delta resolved against the
    # merged base (already-installed base packages satisfy deps instead of reappearing). Action 4
    # runs rpmbuild against the whole stack overlay-merged.
    base = install_packages(ctx, ctx.attrs.distribution, distribution.buildroot_base_packages)
    buildroot = [base]

    if ctx.attrs.build_requires or ctx.attrs.buildroot_deps:
        # Self-hosting: each buildroot_dep is another package we build whose rpms provide some of
        # this package's BuildRequires. Its whole binary-rpm set (the dep's default output dir)
        # goes into the delta as extra packages, so the resolve prefers our build over Fedora's
        # (and can upgrade a base package where a BR pulls our newer build over it). These are real
        # buck deps, so the DAG builds them first (the staircase).
        extra_packages = [d[DefaultInfo].default_outputs[0] for d in ctx.attrs.buildroot_deps]
        buildroot = buildroot + [install_packages(
            ctx,
            ctx.attrs.distribution,
            ctx.attrs.build_requires,
            stack = buildroot,
            extra_packages = extra_packages,
        )]

    rpms = ctx.actions.declare_output("rpms", dir = True)

    # One declared output per binary subpackage (from .bzl's "subpackages"
    # list), so each is an addressable sub-target (:pkg[devel], ...).
    sub_outputs = {s: ctx.actions.declare_output(s + ".rpm") for s in ctx.attrs.subpackages}

    build = cmd_args(
        chroot_run(engine = distribution.engine, exe = distribution.package_format.build),
        "--spec",
        ctx.attrs.spec,
        "--dist",
        ctx.attrs.dist,
        "--release",
        ctx.attrs.release,
        "--source-date-epoch",
        str(ctx.attrs.source_date_epoch),
        "--out",
        rpms.as_output(),
    )
    for layer in buildroot:  # bottom..top: the base lowerdir, then this package's BR delta
        build.add("--lower", layer)
    for src in ctx.attrs.srcs:
        build.add(cmd_args("--source", src))
    for s, out in sub_outputs.items():
        build.add("--subpackage", cmd_args(out.as_output(), format = s + "={}"))
    ctx.actions.run(build, category = "rpmbuild")

    sub_targets = {s: [DefaultInfo(default_output = out)] for s, out in sub_outputs.items()}
    sub_targets["buildroot"] = [DefaultInfo(default_outputs = buildroot)]
    return [DefaultInfo(default_output = rpms, sub_targets = sub_targets)]

_rpm_package = rule(
    impl = _rpm_package_impl,
    attrs = {
        "spec": attrs.source(doc = "the rpm spec file (derived from `package` by the macro)"),
        "package": attrs.string(doc = "the rpm package Name: (distinct from the buck target name)"),
        "srcs": attrs.list(attrs.source(), default = [], doc = "Source/Patch files"),
        "release": attrs.string(doc = "dist-stripped Release base; the build freezes %autorelease = <release>%{?dist}"),
        "subpackages": attrs.list(attrs.string(), doc = "declared binary subpackage names (the %package list)"),
        "build_requires": attrs.list(attrs.string(), default = [], doc = "the package's BuildRequires, installed as a delta over the shared base buildroot"),
        "buildroot_deps": attrs.list(attrs.dep(), default = [], doc = "our packages whose rpms overlay the buildroot (self-hosted BRs)"),
        "source_date_epoch": attrs.int(doc = "per-package SDE from the changelog"),
        "dist": attrs.string(default = ".aos"),
        "distribution": attrs.dep(providers = [DistributionInfo], doc = "catalog//:<distribution> — buildroot + engine that builds it"),
    },
)

def rpm_package(package: str, spec = None, **kwargs) -> None:
    """Build an rpm package.

    The spec is `<package>.spec` (a rule attr can't default off another attr, so the
    macro derives it at load time)."""
    _rpm_package(package = package, spec = spec or package + ".spec", **kwargs)
