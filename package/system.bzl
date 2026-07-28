"""Native package-system drivers bundled behind one provider."""

PackageSystemInfo = provider(
    doc = "A native package ecosystem and the drivers that operate on it.",
    fields = {
        "extract": provider_field(Dependency),
        "snapshot": provider_field(Dependency),
        "install": provider_field(Dependency),
        "pkgdb": provider_field(Dependency),
        "index": provider_field(Dependency),
        "package_suffix": provider_field(str),
        "plan": provider_field(Dependency),
        "build": provider_field(Dependency),
        "database_paths": provider_field(list[str]),
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
            index = ctx.attrs.index,
            package_suffix = ctx.attrs.package_suffix,
            plan = ctx.attrs.plan,
            build = ctx.attrs.build,
            database_paths = ctx.attrs.database_paths,
        ),
    ]

package_system = rule(
    impl = _package_system_impl,
    attrs = {
        "extract": attrs.exec_dep(providers = [RunInfo], doc = "bootstrap payload extractor"),
        "snapshot": attrs.exec_dep(providers = [RunInfo], doc = "repository snapshot generator"),
        "install": attrs.dep(providers = [RunInfo], doc = "install packages into a root"),
        "pkgdb": attrs.dep(providers = [RunInfo], doc = "capture an installed root's package database"),
        "index": attrs.dep(providers = [RunInfo], doc = "write repository metadata"),
        "package_suffix": attrs.string(doc = "file suffix of an installable native package"),
        "plan": attrs.dep(providers = [RunInfo], doc = "resolve package transactions"),
        "build": attrs.dep(providers = [RunInfo], doc = "build a native package"),
        "database_paths": attrs.list(
            attrs.string(),
            doc = "image-root-relative paths the installed package database occupies",
        ),
    },
)
