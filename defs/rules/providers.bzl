"""Shared providers for the engine machinery.

Kept in their own dependency-free module so both python.bzl (the chroot runner)
and distribution.bzl (the rules) can import them without a load cycle.
"""

EngineInfo = provider(
    doc = "The reusable execution environment.",
    fields = {
        "root": provider_field(Artifact),  # the engine root chroot
    },
)

RepoInfo = provider(
    # `dir` is packages + `repodata/` (what the plan resolves against); `packages` are the
    # individual artifacts, keyed by basename to scope an install to its resolved closure.
    doc = "A package repository.",
    fields = {
        "id": provider_field(str),
        "dir": provider_field(Artifact),
        "packages": provider_field(list[Artifact]),
    },
)

DriverInfo = provider(
    # `chroot_python_run` (python.bzl) binds it to an engine as a RunInfo.
    doc = "One chroot-run driver: its entry module + the libs it imports.",
    fields = {
        "main": provider_field(Artifact),
        "deps": provider_field(list[Dependency]),
    },
)

PackageFormatInfo = provider(
    # `extract` provisions chroot1 (a host RunInfo); the rest are DriverInfos a consumer binds
    # to an engine on demand. typing.Any for the DriverInfo fields: see
    # DistributionInfo.package_format.
    doc = "A format's drivers — the per-format plugin.",
    fields = {
        "extract": provider_field(Dependency),  # payload extractor / bootstrap ur-tool (RunInfo)
        "install": provider_field(typing.Any),  # install DriverInfo
        "createrepo": provider_field(typing.Any),  # createrepo DriverInfo
        "plan": provider_field(typing.Any),  # plan DriverInfo
        "build": provider_field(typing.Any),  # package-build DriverInfo
    },
)

DistributionInfo = provider(
    # Pure data (an engine + format plugin + buildroot repos and base packages). The engine
    # root may belong to another distribution (CentOS builds in the Fedora engine root).
    doc = "A distribution build target.",
    fields = {
        "engine": provider_field(EngineInfo),
        # typing.Any, not PackageFormatInfo: provider_field rejects a provider instance under its
        # own type, and reads come back as the generic Provider anyway.
        "package_format": provider_field(typing.Any),
        "buildroot_repositories": provider_field(list[RepoInfo]),
        "buildroot_base_packages": provider_field(list[str]),
    },
)

LayerInfo = provider(
    # `stack` is the ordered ancestor chain (bottom..top): the bottom is a full install root,
    # each entry above it a delta (an overlay upper, deletions encoded as OCI `.wh.` files). A
    # child appends its own delta; a terminal pack overlay-merges the whole stack. `tmpfiles`
    # accumulates each layer's tmpfiles.d snippets down the chain, applied at pack time.
    doc = "An image layer, stored as overlayfs deltas.",
    fields = {
        "stack": provider_field(list[Artifact]),
        "tmpfiles": provider_field(list[str]),
    },
)
