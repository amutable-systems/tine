"""RPM repositories, engines, and package-build rules."""

load("//engine:rules.bzl", "chroot_run", engine_rule = "engine")  # aliased: `engine` is an arg below
load("//package:buildroot.bzl", "BuildrootInfo")
load("//package:install.bzl", "install_packages")
load("//package:manager.bzl", "PackageManagerInfo")
load("//package:repository.bzl", "PackagePoolInfo", "PackagePoolValueInfo", "package_artifact", "package_representation", "remote_repository_base")
load("//package:system.bzl", "PackageSystemInfo")

_RPM_PACKAGE_SYSTEM = "@tine//package_system/rpm:package_system"

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
        # Analysis succeeds before refresh; consuming the empty pool still fails clearly.
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

# Derived RPM representations belong to the repository-owned pool.
remote_repository = rule(
    impl = _remote_repository_impl,
    attrs = {
        "baseurl": attrs.string(),
        "package_system": attrs.dep(providers = [PackageSystemInfo]),
        "priority": attrs.int(default = 99),
        "snapshot": attrs.option(attrs.source(), default = None),
        "_decompress": attrs.exec_dep(
            default = "tine//package_system/rpm:decompress",
            providers = [RunInfo],
        ),
    },
)

def rpm_remote_repository(
        name: str,
        baseurl: str,
        **kwargs) -> None:
    """Declare a repository backed by its optional package-relative snapshot."""
    if not name.endswith(".repository"):
        fail("rpm_remote_repository name must end with '.repository': {}".format(name))
    snapshots = glob([name + ".json"])
    remote_repository(
        name = name,
        baseurl = baseurl,
        package_system = _RPM_PACKAGE_SYSTEM,
        snapshot = snapshots[0] if snapshots else None,
        **kwargs
    )

def rpm_engine(
        name: str,
        packages: list[str],
        repository_universe: str,
        lock: str | None = None,
        **kwargs) -> None:
    """An engine preconfigured for rpm (see the module docstring)."""
    if not name.endswith(".engine"):
        fail("rpm_engine name must end with '.engine': {}".format(name))
    engine_rule(
        name = name,
        packages = packages,
        repository_universe = repository_universe,
        lock = lock or (name + ".json"),
        package_system = _RPM_PACKAGE_SYSTEM,
        **kwargs
    )

def _rpm_package_impl(ctx: AnalysisContext) -> list[Provider]:
    base_buildroot = ctx.attrs.buildroot[BuildrootInfo]
    package_manager_dep = base_buildroot.package_manager
    package_manager = package_manager_dep[PackageManagerInfo]
    system = package_manager.package_system[PackageSystemInfo]

    # Share the base buildroot; add package-specific BuildRequires as a delta.
    buildroot = [base_buildroot.root]

    if ctx.attrs.build_requires or ctx.attrs.buildroot_deps:
        # Self-hosted package outputs outrank upstream while resolving the delta.
        extra_packages = [d[DefaultInfo].default_outputs[0] for d in ctx.attrs.buildroot_deps]
        buildroot = buildroot + [install_packages(
            ctx,
            package_manager_dep,
            ctx.attrs.build_requires,
            stack = buildroot,
            extra_packages = extra_packages,
        )]

    rpms = ctx.actions.declare_output("rpms", dir = True)

    # Declare addressable outputs for every binary subpackage.
    sub_outputs = {s: ctx.actions.declare_output(s + ".rpm") for s in ctx.attrs.subpackages}

    build = cmd_args(
        chroot_run(engine = package_manager.engine, exe = system.build),
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
    for name in sorted(ctx.attrs.macros):
        build.add("--macro", name + "=" + ctx.attrs.macros[name])
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
        "macros": attrs.dict(
            key = attrs.string(),
            value = attrs.string(),
            default = {},
            doc = "extra rpm macro definitions for the build (rpmbuild --define)",
        ),
        "source_date_epoch": attrs.int(doc = "per-package SDE from the changelog"),
        "dist": attrs.string(default = ".aos"),
        "buildroot": attrs.dep(
            providers = [BuildrootInfo],
            doc = "the shared base buildroot",
        ),
    },
)

def rpm_package(package: str, spec = None, **kwargs) -> None:
    """Build an RPM, defaulting the spec to `<package>.spec`."""
    _rpm_package(package = package, spec = spec or package + ".spec", **kwargs)
