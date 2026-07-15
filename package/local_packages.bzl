"""Locally built package universes selected through runtime dependency closures."""

load(":repository.bzl", "LocalPackageInfo")

LocalPackageUniverseInfo = provider(
    doc = "Locally built packages with the runtime metadata to select an install request's closure.",
    fields = {
        # binary package name -> its source package name
        "binary_source": provider_field(dict[str, str]),
        # source package name -> the package target producing its binary package output directory
        "packages": provider_field(dict[str, Dependency]),
        # install-spec capability (binary names and plain explicit Provides) -> providing binaries
        "provides": provider_field(dict[str, list[str]]),
        # binary package name -> local binaries providing any of its runtime requirements
        "requires_edges": provider_field(dict[str, list[str]]),
    },
)

def _local_packages_impl(ctx: AnalysisContext) -> list[Provider]:
    packages = {}
    for dep in ctx.attrs.packages:
        if dep.label.name in packages:
            fail("local_packages: duplicate package target '{}'".format(dep.label))
        packages[dep.label.name] = dep
    for binary, source in ctx.attrs.binary_source.items():
        if source not in packages:
            fail("local_packages: binary '{}' names unknown source package '{}'".format(binary, source))
    return [
        DefaultInfo(),
        LocalPackageUniverseInfo(
            binary_source = ctx.attrs.binary_source,
            packages = packages,
            provides = ctx.attrs.provides,
            requires_edges = ctx.attrs.requires_edges,
        ),
    ]

local_packages = rule(
    impl = _local_packages_impl,
    attrs = {
        "binary_source": attrs.dict(attrs.string(), attrs.string()),
        "packages": attrs.list(
            attrs.dep(providers = [LocalPackageInfo]),
            doc = "built package targets, each named after the source package it builds",
        ),
        "provides": attrs.dict(attrs.string(), attrs.list(attrs.string())),
        "requires_edges": attrs.dict(attrs.string(), attrs.list(attrs.string())),
    },
)

def select_local_packages(info: LocalPackageUniverseInfo, install: list[str]) -> list[Dependency]:
    """The locally built source packages in the runtime closure of an install request.

    Seeds match by binary name or plain explicit Provides after stripping version constraints.
    `@group` specs and capabilities without a local provider resolve from the configured
    repositories instead: the closure only decides which local packages are offered."""
    visited = {}
    frontier = []
    for spec in install:
        if spec.startswith("@"):
            continue
        for binary in info.provides.get(spec.split(" ")[0], []):
            if binary not in visited:
                visited[binary] = True
                frontier.append(binary)

    # Each binary enters the frontier at most once, bounding the walk by the binary count.
    for _ in range(len(info.binary_source) + 1):
        if not frontier:
            break
        binary = frontier.pop()
        for dep in info.requires_edges.get(binary, []):
            if dep not in visited:
                visited[dep] = True
                frontier.append(dep)

    sources = {info.binary_source[binary]: True for binary in visited}
    return [info.packages[source] for source in sorted(sources)]
