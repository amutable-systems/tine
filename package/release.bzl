"""OS-release identity and its native repository universe."""

load(":repository.bzl", "RepositoryUniverseInfo")

OsReleaseInfo = provider(
    doc = "An OS release associated with one native repository universe.",
    fields = {
        "family": provider_field(str),
        "version": provider_field(str),
        "package_system": provider_field(Dependency),
        "repository_universe": provider_field(Dependency),
    },
)

def _os_release_impl(ctx: AnalysisContext) -> list[Provider]:
    universe = ctx.attrs.repository_universe[RepositoryUniverseInfo]

    return [
        DefaultInfo(),
        OsReleaseInfo(
            family = ctx.attrs.family,
            version = ctx.attrs.version,
            package_system = universe.package_system,
            repository_universe = ctx.attrs.repository_universe,
        ),
    ]

_os_release = rule(
    impl = _os_release_impl,
    attrs = {
        "family": attrs.string(doc = "distribution family, such as fedora or centos"),
        "version": attrs.string(doc = "release, suite, or rolling-channel name"),
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
