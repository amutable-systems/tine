# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Shared native package buildroots."""

load(":install.bzl", "install_packages")
load(":manager.bzl", "PackageManagerInfo")

BuildrootInfo = provider(
    doc = "A base root and package manager used to build native packages.",
    fields = {
        "package_manager": provider_field(Dependency),
        "root": provider_field(Artifact),
    },
)

def _buildroot_impl(ctx: AnalysisContext) -> list[Provider]:
    manager = ctx.attrs.package_manager[PackageManagerInfo]
    if manager.local_packages != None:
        fail(
            "buildroot: a package manager with local packages is not supported; buildroot "
            + "contents must come from explicit, cycle-checked package buildroot deps"
        )
    packages = ctx.attrs.packages
    if ctx.attrs.package_set != None:
        if packages:
            fail("buildroot: packages and package_set are mutually exclusive")
        packages = manager.package_sets.get(ctx.attrs.package_set)
        if packages == None:
            fail("buildroot: unknown package set {!r}".format(ctx.attrs.package_set))
    if not packages:
        fail("buildroot: packages or package_set must be specified")
    root = install_packages(ctx, ctx.attrs.package_manager, packages)
    return [
        DefaultInfo(default_output = root),
        BuildrootInfo(
            root = root,
            package_manager = ctx.attrs.package_manager,
        ),
    ]

_buildroot = rule(
    impl = _buildroot_impl,
    attrs = {
        "package_manager": attrs.dep(providers = [PackageManagerInfo]),
        "package_set": attrs.option(
            attrs.string(),
            default = None,
            doc = "release package set installed in every package buildroot",
        ),
        "packages": attrs.list(
            attrs.string(),
            default = [],
            doc = "explicit packages installed in every package buildroot",
        ),
    },
)

def buildroot(name: str, **kwargs) -> None:
    if not name.endswith(".buildroot"):
        fail("buildroot name must end with '.buildroot': {}".format(name))
    _buildroot(name = name, **kwargs)
