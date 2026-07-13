"""Shared native package buildroots."""

load(":install.bzl", "install_packages")
load(":manager.bzl", "PackageManagerInfo")

BuildrootInfo = provider(
    doc = "A base root and package manager used to build native packages.",
    fields = {
        "root": provider_field(Artifact),
        "package_manager": provider_field(Dependency),
    },
)

def _buildroot_impl(ctx: AnalysisContext) -> list[Provider]:
    root = install_packages(ctx, ctx.attrs.package_manager, ctx.attrs.packages)
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
        "packages": attrs.list(attrs.string(), doc = "packages installed in every package buildroot"),
    },
)

def buildroot(name: str, **kwargs) -> None:
    if not name.endswith(".buildroot"):
        fail("buildroot name must end with '.buildroot': {}".format(name))
    _buildroot(
        name = name,
        **kwargs
    )
