"""RPM repositories and package-build rules."""

load("//:specs.bzl", "spec_args")
load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load("//package:buildroot.bzl", "BuildrootInfo")
load("//package:install.bzl", "install_packages")
load("//package:manager.bzl", "PackageManagerInfo")
load(
    "//package:repository.bzl",
    "LocalPackageInfo",
    "PackagePoolInfo",
    "REMOTE_REPOSITORY_ATTRS",
    "RepositoryPin",
    "declare_package_pool",
    "declare_remote_repository",
    "remote_repository_base",
    "snapshot_data",
)
load("//package:system.bzl", "PackageSystemInfo")

_RPM_PACKAGE_SYSTEM = "@tine//package_system/rpm:package_system"

def _materialize_repodata_impl(
    actions: AnalysisActions,
    id: str,
    repo: OutputArtifact,
    snapshot: ArtifactValue,
) -> list[Provider]:
    data = snapshot_data(snapshot, id)
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

def _remote_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    repo = ctx.actions.declare_output("repo", dir = True)
    snapshot = ctx.attrs.snapshot
    if snapshot == None:
        snapshot = ctx.actions.write("empty-snapshot.json", "{}")
    ctx.actions.dynamic_output_new(
        _materialize_repodata(
            id = ctx.label.name,
            repo = repo.as_output(),
            snapshot = snapshot,
        )
    )
    return remote_repository_base(
        ctx,
        baseurl = ctx.attrs.baseurl,
        package_system = ctx.attrs.package_system,
        repo_dir = repo,
    ) + [
        PackagePoolInfo(
            value = declare_package_pool(
                ctx,
                baseurl = ctx.attrs.baseurl,
                engine_locks = ctx.attrs.engine_locks,
                package_system = ctx.attrs.package_system,
                snapshot = snapshot,
            ),
        ),
    ]

_remote_repository = rule(
    impl = _remote_repository_impl,
    attrs = REMOTE_REPOSITORY_ATTRS,
)

def rpm_remote_repository(
    name: str,
    baseurl: str | None = None,
    rpmrepo_mirror: str | None = None,
    rpmrepo_snapshot: str | None = None,
    **kwargs,
) -> None:
    """Declare an RPM repository, optionally pinned to an rpmrepo compose snapshot."""
    if (rpmrepo_mirror == None) != (rpmrepo_snapshot == None):
        fail("rpm_remote_repository requires rpmrepo_mirror and rpmrepo_snapshot together: {}".format(name))
    pin = None
    if rpmrepo_mirror != None:
        pin = RepositoryPin(
            baseurl = rpmrepo_mirror.rstrip("/") + "/" + rpmrepo_snapshot,
            metadata = {"rpmrepo.mirror": rpmrepo_mirror, "rpmrepo.snapshot": rpmrepo_snapshot},
        )
    declare_remote_repository(
        _remote_repository,
        name = name,
        what = "rpm_remote_repository",
        label = "tine:rpm-remote-repository",
        package_system = _RPM_PACKAGE_SYSTEM,
        baseurl = baseurl,
        pin = pin,
        **kwargs,
    )

def _rpm_package_impl(ctx: AnalysisContext) -> list[Provider]:
    base_buildroot = ctx.attrs.buildroot[BuildrootInfo]
    package_manager_dep = base_buildroot.package_manager
    package_manager = package_manager_dep[PackageManagerInfo]
    system = package_manager.package_system[PackageSystemInfo]
    if system.build == None:
        fail("rpm_package: package system {} builds no packages".format(package_manager.package_system.label))

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
        buildroot = buildroot + [
            install_packages(
                ctx,
                package_manager_dep,
                ctx.attrs.build_requires,
                stack = buildroot,
                extra_packages = extra_packages,
            )
        ]

    rpms = ctx.actions.declare_output("rpms", dir = True)

    # Declare addressable outputs for every binary subpackage.
    sub_outputs = {s: ctx.actions.declare_output(s + ".rpm") for s in ctx.attrs.subpackages}

    build = cmd_args(
        chroot_run(engine = package_manager.engine[EngineInfo], exe = system.build),
        spec_args(
            ctx.actions,
            "build.spec.json",
            {
                "dist": ctx.attrs.dist,
                # bottom..top: the base lowerdir, then this package's BuildRequires delta
                "lower": buildroot,
                "out": rpms.as_output(),
                "release": ctx.attrs.release,
                "rpmbuild_options": ctx.attrs.rpmbuild_options,
                "source_date_epoch": ctx.attrs.source_date_epoch,
                "sources": ctx.attrs.srcs,
                "spec_file": ctx.attrs.spec,
                "subpackages": {name: out.as_output() for name, out in sub_outputs.items()},
            },
        ),
    )
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
        "build_requires": attrs.list(
            attrs.string(),
            default = [],
            doc = "the package's BuildRequires, installed as a delta over the shared base buildroot",
        ),
        "buildroot": attrs.dep(
            providers = [BuildrootInfo],
            doc = "the shared base buildroot",
        ),
        "buildroot_deps": attrs.list(
            attrs.dep(providers = [LocalPackageInfo]),
            default = [],
            doc = "our packages whose rpms overlay the buildroot (self-hosted BRs)",
        ),
        "dist": attrs.string(default = ".aos"),
        "package": attrs.string(doc = "the rpm package Name: (distinct from the buck target name)"),
        "release": attrs.string(
            doc = "dist-stripped Release base; the build freezes %autorelease = <release>%{?dist}",
        ),
        "rpmbuild_options": attrs.list(
            attrs.string(),
            default = [],
            doc = "extra rpmbuild CLI options (--with=..., --without=..., --define=...)",
        ),
        "source_date_epoch": attrs.int(doc = "per-package SDE from the changelog"),
        "spec": attrs.source(doc = "the rpm spec file (derived from `package` by the macro)"),
        "srcs": attrs.list(attrs.source(), default = [], doc = "Source/Patch files"),
        "subpackages": attrs.list(
            attrs.string(),
            doc = "declared binary subpackage names (the %package list)",
        ),
    },
)

def rpm_package(package: str, spec: str | None = None, **kwargs) -> None:
    """Build an RPM, defaulting the spec to `<package>.spec`."""
    _rpm_package(package = package, spec = spec or package + ".spec", **kwargs)
