"""Native package-system drivers bundled behind one provider."""

PackageSystemInfo = provider(
    doc = "A native package ecosystem and the drivers that operate on it.",
    fields = {
        "build": provider_field(Dependency | None),
        "database_paths": provider_field(list[str]),
        "extract": provider_field(Dependency),
        "index": provider_field(Dependency),
        "install": provider_field(Dependency),
        "package_suffix": provider_field(str),
        "pkgdb": provider_field(Dependency),
        "plan": provider_field(Dependency),
        "snapshot": provider_field(Dependency),
        "solver_cache": provider_field(bool),
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
            solver_cache = ctx.attrs.solver_cache,
            database_paths = ctx.attrs.database_paths,
        ),
    ]

package_system = rule(
    impl = _package_system_impl,
    attrs = {
        "build": attrs.option(
            attrs.exec_dep(providers = [RunInfo]),
            default = None,
            doc = "build a native package",
        ),
        "database_paths": attrs.list(
            attrs.string(),
            doc = "image-root-relative paths the installed package database occupies",
        ),
        "extract": attrs.exec_dep(providers = [RunInfo], doc = "bootstrap payload extractor"),
        "index": attrs.exec_dep(providers = [RunInfo], doc = "write repository metadata"),
        "install": attrs.exec_dep(providers = [RunInfo], doc = "install packages into a root"),
        "package_suffix": attrs.string(doc = "file suffix a selected native package is named with"),
        "pkgdb": attrs.exec_dep(providers = [RunInfo], doc = "capture an installed root's package database"),
        "plan": attrs.exec_dep(providers = [RunInfo], doc = "resolve package transactions"),
        "snapshot": attrs.exec_dep(providers = [RunInfo], doc = "repository snapshot generator"),
        "solver_cache": attrs.bool(
            default = True,
            doc = "whether the planner reuses metadata prebuilt by its `make-cache` verb",
        ),
    },
)
