"""rpm format defs: rpm_package (build one source rpm into its binary rpms) plus the
rpm-bound catalog wrappers (rpm_remote_repository / rpm_engine / rpm_distribution).

The wrappers preconfigure their format-neutral rule with the rpm plugin
(`@tine//distribution/rpm:package_format`). A repository reads its authoritative snapshot
from `<name>.json` when present; an engine reads its committed transaction from `<name>.json`,
both beside the caller's BUCK. Run `buck run tine//tools:refresh-catalog` to create missing
repository snapshots; seed a new engine transaction with `{}` first. A catalog BUCK composes
them package-relative: a distribution names its engine and repository sibling targets.
"""

load(":distribution.bzl", "DistributionInfo", "distribution", "install_packages", "remote_repository_base")
load(":engine.bzl", "chroot_run", engine_rule = "engine")  # aliased: `engine` is an arg below
load(":package_format.bzl", "PackageFormatInfo")
load(":repo.bzl", "PackagePoolInfo", "PackagePoolValueInfo", "package_artifact", "package_representation")

_RPM_PACKAGE_FORMAT = "@tine//distribution/rpm:package_format"

rpm_metadata = record(
    location = str,
    pkgid = str,
)

RpmPoolInfo = provider(
    doc = "A repository-owned dynamic RPM pool.",
    fields = {"value": provider_field(DynamicValue)},
)

RpmPoolValueInfo = provider(
    doc = "Resolved RPM-specific package metadata and artifacts keyed by pkgid.",
    fields = {"rpms": provider_field(dict[str, package_artifact])},
)

def _materialize_repository_impl(
        actions: AnalysisActions,
        baseurl: str,
        decompress: RunInfo,
        id: str,
        repo: OutputArtifact,
        snapshot: ArtifactValue) -> list[Provider]:
    data = snapshot.read_json()
    if type(data) != type({}):
        fail("repository '{}' snapshot is not an object; run refresh-catalog".format(id))
    repomd_xml = data.get("repomd", "")
    if not repomd_xml:
        fail(
            "repository '{}' is not locked yet (snapshot missing or empty); run refresh-catalog".format(id),
        )

    tree = {"repodata/repomd.xml": actions.write("repomd.xml", repomd_xml)}
    for stream in data.get("streams", []):
        out_name = stream["out"]
        out = actions.declare_output(out_name)
        actions.download_file(
            out,
            stream["url"],
            sha256 = stream["sha256"],
            size_bytes = stream["size"],
        )
        tree["repodata/" + out_name] = out
    actions.copied_dir(repo, tree)

    rpms = {}
    for pkgid, package in data.get("packages", {}).items():
        location = package["location"]
        raw = actions.declare_output("packages", pkgid + ".rpm", has_content_based_path = True)
        actions.download_file(
            raw,
            baseurl.rstrip("/") + "/" + location.lstrip("/"),
            sha256 = pkgid,
            size_bytes = package["size"],
        )
        payload = actions.declare_output("payloads", pkgid + ".cpio", has_content_based_path = True)
        actions.run(
            cmd_args(decompress, raw, payload.as_output()),
            category = "rpm_payload",
            identifier = pkgid,
        )
        rpms[pkgid] = package_artifact(
            artifact = raw,
            metadata = rpm_metadata(location = location, pkgid = pkgid),
            name = location.rsplit("/", 1)[-1],
            representations = {
                "payload": package_representation(artifact = payload, suffix = ".cpio"),
            },
        )
    return [
        PackagePoolValueInfo(packages = rpms),
        RpmPoolValueInfo(rpms = rpms),
    ]

_materialize_repository = dynamic_actions(
    impl = _materialize_repository_impl,
    attrs = {
        "baseurl": dynattrs.value(str),
        "decompress": dynattrs.value(RunInfo),
        "id": dynattrs.value(str),
        "repo": dynattrs.output(),
        "snapshot": dynattrs.artifact_value(),
    },
)

def _remote_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    repo = ctx.actions.declare_output("repo", dir = True)
    snapshot = ctx.attrs.snapshot
    if snapshot == None:
        # Keep the repository target analyzable before its first refresh. The dynamic pool
        # still fails as unlocked if a consumer requests it; `[snapshot]` does not resolve it.
        snapshot = ctx.actions.write("empty-snapshot.json", "{}")
    pool = ctx.actions.dynamic_output_new(_materialize_repository(
        baseurl = ctx.attrs.baseurl,
        decompress = ctx.attrs._decompress[RunInfo],
        id = ctx.label.name,
        repo = repo.as_output(),
        snapshot = snapshot,
    ))
    return remote_repository_base(ctx, repo) + [
        PackagePoolInfo(value = pool),
        RpmPoolInfo(value = pool),
    ]

# The pool is deliberately the RPM-specific ownership boundary: payload decompression,
# signature checks, reflink transforms, or future derived representations belong beside each
# raw action and can be exposed through the RpmPoolInfo dynamic value without complicating
# transaction selection.
remote_repository = rule(
    impl = _remote_repository_impl,
    attrs = {
        "baseurl": attrs.string(),
        "package_format": attrs.dep(providers = [PackageFormatInfo]),
        "priority": attrs.int(default = 99),
        "snapshot": attrs.option(attrs.source(), default = None),
        "_decompress": attrs.exec_dep(
            default = "tine//distribution/rpm:decompress",
            providers = [RunInfo],
        ),
    },
)

def rpm_remote_repository(
        name: str,
        baseurl: str,
        **kwargs) -> None:
    """Declare one repository target owning its repodata and authoritative RPM pool.

    The package-relative `<name>.json` is one authoritative unit: filtered repodata plus
    a `packages` map keyed by pkgid. It may be absent before the first catalog refresh. The
    target reads it dynamically, owns one independent raw artifact per entry, and exposes
    the generic pool and RPM-specific representations.
    """
    snapshots = glob([name + ".json"])
    remote_repository(
        name = name,
        baseurl = baseurl,
        package_format = _RPM_PACKAGE_FORMAT,
        snapshot = snapshots[0] if snapshots else None,
        **kwargs
    )

def rpm_engine(
        name: str,
        packages: list[str],
        repositories: list[str],
        lock: str | None = None,
        **kwargs) -> None:
    """An engine preconfigured for rpm (see the module docstring)."""
    engine_rule(
        name = name,
        packages = packages,
        repositories = repositories,
        lock = lock or (name + ".json"),
        package_format = _RPM_PACKAGE_FORMAT,
        **kwargs
    )

def rpm_distribution(
        name: str,
        engine: str,
        repositories: list[str],
        buildroot: list[str],
        **kwargs) -> None:
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
        "release": attrs.string(
            doc = "dist-stripped Release base; the build freezes %autorelease = <release>%{?dist}",
        ),
        "subpackages": attrs.list(
            attrs.string(),
            doc = "declared binary subpackage names (the %package list)",
        ),
        "build_requires": attrs.list(
            attrs.string(),
            default = [],
            doc = "the package's BuildRequires, installed as a delta over the shared base buildroot",
        ),
        "buildroot_deps": attrs.list(
            attrs.dep(),
            default = [],
            doc = "our packages whose rpms overlay the buildroot (self-hosted BRs)",
        ),
        "source_date_epoch": attrs.int(doc = "per-package SDE from the changelog"),
        "dist": attrs.string(default = ".aos"),
        "distribution": attrs.dep(
            providers = [DistributionInfo],
            doc = "catalog//:<distribution> — buildroot + engine that builds it",
        ),
    },
)

def rpm_package(package: str, spec = None, **kwargs) -> None:
    """Build an rpm package.

    The spec is `<package>.spec` (a rule attr can't default off another attr, so the
    macro derives it at load time)."""
    _rpm_package(package = package, spec = spec or package + ".spec", **kwargs)
