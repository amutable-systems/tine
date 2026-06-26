"""Shared providers for the engine machinery.

Kept in their own dependency-free module so both python.bzl (the chroot runner)
and distribution.bzl (the rules) can import them without a load cycle.
"""

# The reusable execution environment.
EngineInfo = provider(fields = {
    "root": provider_field(Artifact),  # the engine root chroot
})

# A package repository: `dir` (packages + `repodata/`, what the plan resolves against)
# plus the individual package artifacts, keyed by basename to scope an install to its
# resolved closure.
RepoInfo = provider(fields = {
    "id": provider_field(str),
    "dir": provider_field(Artifact),
    "packages": provider_field(list[Artifact]),
})

# One chroot-run driver: its entry module + the libs it imports. `chroot_python_run`
# (python.bzl) binds it to an engine as a RunInfo.
DriverInfo = provider(fields = {
    "main": provider_field(Artifact),
    "deps": provider_field(list[Dependency]),
})

# A format's drivers — the per-format plugin. `extract` provisions chroot1 (a host
# RunInfo); the rest are DriverInfos a consumer binds to an engine on demand.
# typing.Any for the DriverInfo fields: see DistributionInfo.package_format.
PackageFormatInfo = provider(fields = {
    "extract": provider_field(Dependency),  # payload extractor / bootstrap ur-tool (RunInfo)
    "install": provider_field(typing.Any),  # install DriverInfo
    "createrepo": provider_field(typing.Any),  # createrepo DriverInfo
    "plan": provider_field(typing.Any),  # plan DriverInfo
    "build": provider_field(typing.Any),  # package-build DriverInfo
})

# A distribution build target: pure data (an engine + format plugin + buildroot repos and
# base packages). The engine root may belong to another distribution (CentOS builds in the
# Fedora engine root).
DistributionInfo = provider(fields = {
    "engine": provider_field(EngineInfo),
    # typing.Any, not PackageFormatInfo: provider_field rejects a provider instance under its
    # own type, and reads come back as the generic Provider anyway.
    "package_format": provider_field(typing.Any),
    "buildroot_repositories": provider_field(list[RepoInfo]),
    "buildroot_base_packages": provider_field(list[str]),
})

# An image layer: one root-filesystem tree. Layers chain (a layer's tree is the next's
# parent); a terminal pack turns a tree into an archive.
LayerInfo = provider(fields = {
    "tree": provider_field(Artifact),
})
