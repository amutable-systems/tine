"""The package format plugin: a format's drivers, bundled as one target."""

PackageFormatInfo = provider(
    # `extract` bootstraps the engine (a host RunInfo — no chroot exists yet) and
    # `snapshot` pins repository metadata + its package inventory on the host at refresh; the rest are
    # python_bootstrap_binary deps a consumer binds to an engine on demand (chroot_run).
    doc = "A format's drivers — the per-format plugin.",
    fields = {
        "extract": provider_field(Dependency),  # payload extractor / bootstrap ur-tool
        "snapshot": provider_field(Dependency),  # authoritative repository snapshot generator
        "install": provider_field(Dependency),  # cmdline-install a package set into a root
        "create_repository": provider_field(Dependency),  # repository metadata writer
        # Resolver → transaction; also the engine's `[resolve]` and solver-cache prebuild.
        "plan": provider_field(Dependency),
        "build": provider_field(Dependency),  # package builder
    },
)

def _package_format_impl(ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(),
        PackageFormatInfo(
            extract = ctx.attrs.extract,
            snapshot = ctx.attrs.snapshot,
            install = ctx.attrs.install,
            create_repository = ctx.attrs.create_repository,
            plan = ctx.attrs.plan,
            build = ctx.attrs.build,
        ),
    ]

# A package format plugin: every format-specific driver (a python_bootstrap_binary a
# consumer binds to an engine via chroot_run), declared in that format's subdir
# (`//distribution/<format>:package_format`) so the orchestration layer selects them by
# `data["package_format"]` without naming a format.
package_format = rule(
    impl = _package_format_impl,
    attrs = {
        "extract": attrs.exec_dep(
            providers = [RunInfo],
            doc = "the payload extractor / bootstrap ur-tool (Action B)",
        ),
        "snapshot": attrs.exec_dep(
            providers = [RunInfo],
            doc = "pin a repository's metadata and authoritative package index (host, refresh)",
        ),
        "install": attrs.dep(
            providers = [RunInfo],
            doc = "the install driver (cmdline install a package set into a root)",
        ),
        "create_repository": attrs.dep(
            providers = [RunInfo],
            doc = "write repository metadata for a set of package artifacts",
        ),
        "plan": attrs.dep(
            providers = [RunInfo],
            doc = "resolver → transaction; also the engine-closure resolver at refresh",
        ),
        "build": attrs.dep(providers = [RunInfo], doc = "the package-build driver"),
    },
)
