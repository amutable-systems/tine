"""OS-release identity and its native repository universe."""

load(":repository.bzl", "RepositoryUniverseInfo")

OsReleaseInfo = provider(
    doc = "An OS release associated with one native repository universe.",
    fields = {
        "package_sets": provider_field(dict[str, list[str]]),
        "package_system": provider_field(Dependency),
        "repository_universe": provider_field(Dependency),
    },
)

def _os_release_impl(ctx: AnalysisContext) -> list[Provider]:
    universe = ctx.attrs.repository_universe[RepositoryUniverseInfo]
    package_sets = {}
    for name, packages in ctx.attrs.package_sets.items():
        if not name or not packages:
            fail("os_release: package set names and contents cannot be empty")
        package_sets[name] = sorted(packages)

    return [
        DefaultInfo(),
        OsReleaseInfo(
            package_sets = package_sets,
            package_system = universe.package_system,
            repository_universe = ctx.attrs.repository_universe,
        ),
    ]

_os_release = rule(
    impl = _os_release_impl,
    attrs = {
        "package_sets": attrs.dict(
            attrs.string(),
            attrs.list(attrs.string()),
            default = {},
            doc = "named native package sets supplied by release policy",
        ),
        "repository_universe": attrs.dep(providers = [RepositoryUniverseInfo]),
    },
)

def os_release(name: str, **kwargs) -> None:
    if not name.endswith(".release"):
        fail("os_release name must end with '.release': {}".format(name))
    _os_release(
        name = name,
        **kwargs
    )
