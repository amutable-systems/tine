"""Native package-system drivers bundled behind one provider."""

PackageSystemInfo = provider(
    doc = "A native package ecosystem and the drivers that operate on it.",
    fields = {
        "extract": provider_field(Dependency),
        "snapshot": provider_field(Dependency),
        "install": provider_field(Dependency),
        "pkgdb": provider_field(Dependency),
        "createrepo": provider_field(Dependency),
        "plan": provider_field(Dependency),
        "build": provider_field(Dependency),
    },
)

def _package_system_impl(ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(),
        PackageSystemInfo(
            extract = ctx.attrs.extract,
            snapshot = ctx.attrs.snapshot,
            install = ctx.attrs.install,
            pkgdb = ctx.attrs.pkgdb,
            createrepo = ctx.attrs.createrepo,
            plan = ctx.attrs.plan,
            build = ctx.attrs.build,
        ),
    ]

package_system = rule(
    impl = _package_system_impl,
    attrs = {
        "extract": attrs.exec_dep(providers = [RunInfo], doc = "bootstrap payload extractor"),
        "snapshot": attrs.exec_dep(providers = [RunInfo], doc = "repository snapshot generator"),
        "install": attrs.dep(providers = [RunInfo], doc = "install packages into a root"),
        "pkgdb": attrs.dep(providers = [RunInfo], doc = "capture an installed root's package database"),
        "createrepo": attrs.dep(providers = [RunInfo], doc = "write repository metadata"),
        "plan": attrs.dep(providers = [RunInfo], doc = "resolve package transactions"),
        "build": attrs.dep(providers = [RunInfo], doc = "build a native package"),
    },
)
