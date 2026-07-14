"""Repositories assembled from package artifacts in the build graph."""

load("//engine:rules.bzl", "EngineInfo", "chroot_run")
load(":manager.bzl", "PackageManagerInfo")
load(":repository.bzl", "LocalPackageRepositoryInfo", "PackageRepositoryInfo")
load(":system.bzl", "PackageSystemInfo")

def _local_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    package_manager = ctx.attrs.package_manager[PackageManagerInfo]
    package_dirs = ctx.attrs.packages
    if not package_dirs:
        fail("local_repository: packages must not be empty")
    repo = ctx.actions.declare_output("repo", dir = True)
    cmd = cmd_args(
        chroot_run(
            engine = package_manager.engine[EngineInfo],
            exe = package_manager.package_system[PackageSystemInfo].createrepo,
        ),
        "--out",
        repo.as_output(),
    )
    for package_dir in package_dirs:
        cmd.add("--packages-dir", package_dir)
    ctx.actions.run(cmd, category = "createrepo")

    return [
        DefaultInfo(default_output = repo),
        PackageRepositoryInfo(
            id = ctx.label.name,
            dir = repo,
            package_system = package_manager.package_system,
            priority = ctx.attrs.priority,
        ),
        LocalPackageRepositoryInfo(package_dirs = package_dirs),
    ]

_local_repository = rule(
    impl = _local_repository_impl,
    attrs = {
        "package_manager": attrs.dep(providers = [PackageManagerInfo]),
        "packages": attrs.list(
            attrs.source(allow_directory = True),
            doc = "package output directories to publish",
        ),
        "priority": attrs.int(default = 50),
    },
)

def local_repository(name: str, **kwargs) -> None:
    """Publish package artifacts as a repository for install operations."""
    if not name.endswith(".repository"):
        fail("local_repository name must end with '.repository': {}".format(name))
    _local_repository(
        name = name,
        **kwargs
    )
