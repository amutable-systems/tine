"""RPM repositories and package-build rules."""

load("//:specs.bzl", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//git:defs.bzl", "git")
load("//package:buildroot.bzl", "BuildrootInfo")
load("//package:install.bzl", "install_packages")
load("//package:manager.bzl", "PackageManagerInfo")
load("//package:repository.bzl", "LocalPackageInfo", "RepositoryPin", "declare_remote_repository")
load("//package:system.bzl", "PackageSystemInfo")
load("//project:defs.bzl", "project")

PACKAGE_SYSTEM = "@tine//package_system/rpm:package_system"
_PRIVATE = "__tine"

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
        name = name,
        what = "rpm_remote_repository",
        label = "tine:rpm-remote-repository",
        package_system = PACKAGE_SYSTEM,
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

    source_tree = ctx.attrs.source_tree
    if ctx.attrs.copy_source_tree:
        source_tree = project.digested(ctx.actions, _PRIVATE + "/source", source_tree)
    rpms = ctx.actions.declare_output("rpms", dir = True)
    build_dir = ctx.actions.declare_output(_PRIVATE + "/build", dir = True) if ctx.attrs.configured_dev else None
    in_place_spec = ctx.attrs.in_place_spec if source_tree != None else None
    spec_file = ctx.attrs.spec
    if source_tree != None and in_place_spec != None:
        spec_file = source_tree.project(in_place_spec)
    if spec_file == None:
        fail("rpm_package: a regular build requires a spec file")

    # Declare addressable outputs for every binary subpackage.
    sub_outputs = {s: ctx.actions.declare_output(s + ".rpm") for s in ctx.attrs.subpackages}

    build = cmd_args(
        box_run(box = package_manager.box[BoxInfo], exe = system.build),
        spec_args(
            ctx.actions,
            "build.spec.json",
            {
                "build_dir": build_dir.as_output() if build_dir != None else None,
                "dist": ctx.attrs.dist,
                "in_place_rpmbuild_options": ctx.attrs.in_place_rpmbuild_options if source_tree != None else [],
                # bottom..top: the base lowerdir, then this package's BuildRequires delta
                "lower": buildroot,
                "out": rpms.as_output(),
                "release": ctx.attrs.release,
                "rpmbuild_options": ctx.attrs.rpmbuild_options,
                "source_date_epoch": ctx.attrs.source_date_epoch,
                "source_tree": source_tree,
                "sources": ctx.attrs.srcs if in_place_spec == None else [],
                "spec_file": spec_file,
                "subpackages": {name: out.as_output() for name, out in sub_outputs.items()},
            },
        ),
    )
    ctx.actions.run(
        build,
        category = "rpmbuild",
        no_outputs_cleanup = ctx.attrs.configured_dev,
        # Live checkouts can contain ignored files, unlike fetched trees and explicit archive inputs.
        allow_cache_upload = not ctx.attrs.configured_dev and (source_tree == None or not source_tree.is_source),
    )

    sub_targets = {s: [DefaultInfo(default_output = out)] for s, out in sub_outputs.items()}
    sub_targets["buildroot"] = [DefaultInfo(default_outputs = buildroot)]
    if build_dir != None:
        sub_targets["build"] = [DefaultInfo(default_output = build_dir)]
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
        "configured_dev": attrs.bool(
            doc = "whether project configuration selects this package for dev mode",
        ),
        "copy_source_tree": attrs.bool(
            doc = "source_tree is a directory of this package, built from the copy Buck digested",
        ),
        "dist": attrs.string(default = ".aos"),
        "in_place_rpmbuild_options": attrs.list(
            attrs.string(),
            default = [],
            doc = "extra rpmbuild CLI options used only with a source-tree override",
        ),
        "in_place_spec": attrs.option(
            attrs.string(),
            default = None,
            doc = "source-tree-relative spec whose directory supplies in-place Source/Patch files",
        ),
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
        "source_tree": attrs.option(
            attrs.source(allow_directory = True),
            default = None,
            doc = "a prepared tree built in place instead of running %prep",
        ),
        "spec": attrs.option(attrs.source(), default = None, doc = "the rpm spec file (derived from `package` by the macro)"),
        "srcs": attrs.list(attrs.source(), default = [], doc = "Source/Patch files"),
        "subpackages": attrs.list(
            attrs.string(),
            doc = "declared binary subpackage names (the %package list)",
        ),
    },
)

def rpm_package(
    name: str,
    package: str,
    spec: str | None = None,
    dev: bool | None = None,
    in_place_rpmbuild_options: list[str] | None = None,
    in_place_spec: str | None = None,
    source_tree: str | None = None,
    **kwargs,
) -> None:
    """Build an RPM from archives or a prepared Git tree, with a local checkout override."""
    if source_tree == None:
        source = name + ".source"
        populated = git.checkout(name = source)
        source_tree = ":" + source if populated else None
    if spec != None and source_tree != None and project.is_directory(source_tree) and spec.startswith(source_tree.rstrip("/") + "/"):
        # The tree is built from a copy, where a path into the directory no longer points.
        fail("rpm_package {}: select a spec inside source_tree with in_place_spec".format(name))
    if spec == None and (source_tree == None or in_place_spec == None):
        spec = package + ".spec"
    _rpm_package(
        name = name,
        package = package,
        spec = spec,
        configured_dev = project.is_dev(name, source = source_tree, override = dev),
        copy_source_tree = source_tree != None and project.is_directory(source_tree),
        source_tree = source_tree,
        in_place_rpmbuild_options = in_place_rpmbuild_options,
        in_place_spec = in_place_spec,
        **kwargs,
    )
