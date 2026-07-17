"""RPM repositories and package-build rules."""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load("//package:buildroot.bzl", "BuildrootInfo")
load("//package:install.bzl", "install_packages")
load("//package:manager.bzl", "PackageManagerInfo")
load("//package:repository.bzl", "LocalPackageInfo", "PackageArtifactInfo", "PackagePoolInfo", "PackagePoolValueInfo", "PackageRepresentationInfo", "remote_repository_base")
load("//package:system.bzl", "PackageSystemInfo")

_RPM_PACKAGE_SYSTEM = "@tine//package_system/rpm:package_system"

def _snapshot_data(snapshot: ArtifactValue, id: str) -> dict:
    data = snapshot.read_json()
    if type(data) != type({}):
        fail("repository '{}' snapshot is not an object; run refresh-catalog".format(id))
    return data

def _materialize_repodata_impl(
        actions: AnalysisActions,
        id: str,
        repo: OutputArtifact,
        snapshot: ArtifactValue) -> list[Provider]:
    data = _snapshot_data(snapshot, id)
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
    return []

_materialize_repodata = dynamic_actions(
    impl = _materialize_repodata_impl,
    attrs = {
        "id": dynattrs.value(str),
        "repo": dynattrs.output(),
        "snapshot": dynattrs.artifact_value(),
    },
)

def _retained_packages(id: str, engine_locks: list[ArtifactValue]) -> dict[str, dict]:
    retained = {}
    for lock in engine_locks:
        for entry in lock.read_json():
            if entry["source"] != "repo" or entry["repo"] != id:
                continue

            pkgid = entry["pkgid"]
            package = {"size": entry["size"], "url": entry["url"]}
            previous = retained.get(pkgid)
            if previous != None and previous["size"] != package["size"]:
                fail("repository '{}': retained package {} has conflicting sizes".format(id, pkgid))

            # More than one engine may retain the same content through different mirrors.
            if previous == None or package["url"] < previous["url"]:
                retained[pkgid] = package
    return retained

def _materialize_package_pool_impl(
        actions: AnalysisActions,
        baseurl: str,
        decompress: RunInfo,
        engine_locks: list[ArtifactValue],
        id: str,
        snapshot: ArtifactValue) -> list[Provider]:
    data = _snapshot_data(snapshot, id)
    packages = _retained_packages(id, engine_locks)
    for pkgid, package in data.get("packages", {}).items():
        retained = packages.get(pkgid)
        if retained != None and retained["size"] != package["size"]:
            fail("repository '{}': package {} has conflicting snapshot and lock sizes".format(id, pkgid))

        # Prefer the repository's current route while it still advertises this content. The lock
        # transport remains the fallback after the package leaves the current snapshot.
        packages[pkgid] = {
            "size": package["size"],
            "url": baseurl.rstrip("/") + "/" + package["location"],
        }

    rpms = {}
    for pkgid, package in packages.items():
        url = package["url"]
        raw = actions.declare_output("packages", pkgid + ".rpm", has_content_based_path = True)
        actions.download_file(
            raw,
            url,
            sha256 = pkgid,
            size_bytes = package["size"],
        )
        payload = actions.declare_output("payloads", pkgid + ".cpio", has_content_based_path = True)
        actions.run(
            cmd_args(decompress, raw, payload.as_output()),
            category = "rpm_payload",
            identifier = pkgid,
        )
        rpms[pkgid] = PackageArtifactInfo(
            artifact = raw,
            name = url.rsplit("/", 1)[-1],
            representations = {
                "payload": PackageRepresentationInfo(artifact = payload, suffix = ".cpio"),
            },
        )
    return [PackagePoolValueInfo(packages = rpms)]

_materialize_package_pool = dynamic_actions(
    impl = _materialize_package_pool_impl,
    attrs = {
        "baseurl": dynattrs.value(str),
        "decompress": dynattrs.value(RunInfo),
        "engine_locks": dynattrs.list(dynattrs.artifact_value()),
        "id": dynattrs.value(str),
        "snapshot": dynattrs.artifact_value(),
    },
)

def _remote_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    repo = ctx.actions.declare_output("repo", dir = True)
    snapshot = ctx.attrs.snapshot
    if snapshot == None:
        snapshot = ctx.actions.write("empty-snapshot.json", "{}")
    ctx.actions.dynamic_output_new(_materialize_repodata(
        id = ctx.label.name,
        repo = repo.as_output(),
        snapshot = snapshot,
    ))
    pool = ctx.actions.dynamic_output_new(_materialize_package_pool(
        baseurl = ctx.attrs.baseurl,
        decompress = ctx.attrs._decompress[RunInfo],
        engine_locks = ctx.attrs.engine_locks,
        id = ctx.label.name,
        snapshot = snapshot,
    ))
    return remote_repository_base(ctx, repo) + [
        PackagePoolInfo(value = pool),
    ]

_remote_repository = rule(
    impl = _remote_repository_impl,
    attrs = {
        "baseurl": attrs.string(),
        "engine_locks": attrs.list(
            attrs.source(),
            default = [],
            doc = "frozen engine transactions whose remote package transports remain available",
        ),
        "labels": attrs.list(attrs.string(), default = []),
        "package_system": attrs.dep(providers = [PackageSystemInfo]),
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
        labels: list[str] = [],
        **kwargs) -> None:
    """Declare a repository backed by its optional package-relative snapshot."""
    if not name.endswith(".repository"):
        fail("rpm_remote_repository name must end with '.repository': {}".format(name))
    snapshot_name = name[:-len(".repository")]
    snapshots = glob(["snapshot/repo/" + snapshot_name + ".json"])
    if len(snapshots) > 1:
        fail("rpm_remote_repository {} has multiple snapshots: {}".format(name, snapshots))
    snapshot = snapshots[0] if snapshots else None
    _remote_repository(
        name = name,
        baseurl = baseurl,
        engine_locks = glob(["snapshot/engine/*.json"]),
        labels = ["tine:rpm-remote-repository"] + labels,
        package_system = _RPM_PACKAGE_SYSTEM,
        snapshot = snapshot,
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
        extra_packages = []
        for dependency in ctx.attrs.buildroot_deps:
            info = dependency[LocalPackageInfo]
            if info.package_system.label != package_manager.package_system.label:
                fail(
                    "rpm_package: buildroot dependency {} uses package system {}, expected {}".format(
                        dependency.label,
                        info.package_system.label,
                        package_manager.package_system.label,
                    ),
                )
            extra_packages.append(info.packages)
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
        chroot_run(engine = package_manager.engine[EngineInfo], exe = system.build),
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
    for opt in ctx.attrs.rpmbuild_options:
        # argparse rejects an option value that itself starts with '-', so don't use space separator
        build.add("--rpmbuild-option=" + opt)
    ctx.actions.run(build, category = "rpmbuild")

    sub_targets = {s: [DefaultInfo(default_output = out)] for s, out in sub_outputs.items()}
    sub_targets["buildroot"] = [DefaultInfo(default_outputs = buildroot)]
    return [
        DefaultInfo(default_output = rpms, sub_targets = sub_targets),
        LocalPackageInfo(
            package_system = package_manager.package_system,
            packages = rpms,
        ),
    ]

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
            attrs.dep(providers = [LocalPackageInfo]),
            default = [],
            doc = "our packages whose rpms overlay the buildroot (self-hosted BRs)",
        ),
        "rpmbuild_options": attrs.list(
            attrs.string(),
            default = [],
            doc = "extra rpmbuild CLI options (--with=..., --without=..., --define=...)",
        ),
        "source_date_epoch": attrs.int(doc = "per-package SDE from the changelog"),
        "dist": attrs.string(default = ".aos"),
        "buildroot": attrs.dep(
            providers = [BuildrootInfo],
            doc = "the shared base buildroot",
        ),
    },
)

def rpm_package(package: str, spec: str | None = None, **kwargs) -> None:
    """Build an RPM, defaulting the spec to `<package>.spec`."""
    _rpm_package(package = package, spec = spec or package + ".spec", **kwargs)
