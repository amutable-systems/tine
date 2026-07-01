"""rpm format defs: rpm_package (build one source rpm into its binary rpms) plus the
rpm-bound catalog wrappers (rpm_remote_repository / rpm_engine / rpm_distribution).

The wrappers preconfigure their format-neutral rule with the rpm plugin
(`@tine//distribution/rpm:package_format`) and default each lock to the `<name>.json`
fragment beside the caller's BUCK (seed it with `{}`; `buck run tine//tools:refresh-catalog` fills it).
A catalog BUCK composes them package-relative: a distribution names its engine and
repository sibling targets.
"""

load(":distribution.bzl", "DistributionInfo", "assemble_root", "distribution", "remote_repository")
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

    buildroot = assemble_root(
        ctx,
        ctx.attrs.distribution,
        distribution.buildroot_base_packages + ctx.attrs.build_requires,
    )

    rpms = ctx.actions.declare_output("rpms", dir = True)
    topdir = ctx.actions.declare_output("topdir", dir = True)

    # One declared output per binary subpackage (from .bzl's "subpackages"
    # list), so each is an addressable sub-target (:pkg[devel], ...).
    sub_outputs = {s: ctx.actions.declare_output(s + ".rpm") for s in ctx.attrs.subpackages}

    build = cmd_args(
        chroot_run(engine = distribution.engine, exe = distribution.package_format.build),
        "--buildroot",
        buildroot,
        "--spec",
        ctx.attrs.spec,
        "--dist",
        ctx.attrs.dist,
        "--version",
        ctx.attrs.version,
        "--release",
        ctx.attrs.release,
        "--source-date-epoch",
        str(ctx.attrs.source_date_epoch),
        "--topdir",
        topdir.as_output(),
        "--out",
        rpms.as_output(),
    )
    for src in ctx.attrs.srcs:
        build.add(cmd_args("--source", src))
    for s, out in sub_outputs.items():
        build.add("--subpackage", cmd_args(out.as_output(), format = s + "={}"))
    ctx.actions.run(build, category = "rpmbuild")

    sub_targets = {s: [DefaultInfo(default_output = out)] for s, out in sub_outputs.items()}
    sub_targets["buildroot"] = [DefaultInfo(default_output = buildroot)]
    sub_targets["topdir"] = [DefaultInfo(default_output = topdir)]
    return [DefaultInfo(default_output = rpms, sub_targets = sub_targets)]

_rpm_package = rule(
    impl = _rpm_package_impl,
    attrs = {
        "spec": attrs.source(doc = "the rpm spec file (derived from `package` by the macro)"),
        "package": attrs.string(doc = "the rpm package Name: (distinct from the buck target name)"),
        "srcs": attrs.list(attrs.source(), default = [], doc = "Source/Patch files"),
        "version": attrs.string(doc = "package version (for subpackage NVR matching)"),
        "release": attrs.string(doc = "dist-stripped Release base; the build freezes %autorelease = <release>%{?dist}"),
        "subpackages": attrs.list(attrs.string(), doc = "declared binary subpackage names (the %package list)"),
        "build_requires": attrs.list(attrs.string(), default = [], doc = "extra BR caps beyond the buildroot base (catalog-only for now)"),
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
