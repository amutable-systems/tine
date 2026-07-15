"""Build RPM targets from generated package metadata and self-hosting edges."""

load("@prelude//:native.bzl", "native")
load(":rules.bzl", "rpm_package")

# Keep these records aligned with the importer's generated schema.
# buildifier: disable=name-conventions  (a record *type*, conventionally UpperCamelCase)
SourceMetadata = record(
    url = str,
    sha256sum = str,
    size = int,  # archive size in bytes
)

# buildifier: disable=name-conventions
SrcpkgMetadata = record(
    build_requires = dict[str, list[str]],  # "_all" + per-arch conditional extras
    # producing build arch → pkgname → {Files, Requires, Recommends, Provides}
    binaries = dict[str, dict[str, dict[str, list[str]]]],
    sources = list[SourceMetadata],
    version = str,  # main-package version; recorded metadata, not consumed by the build (spec drives it)
    release = str,
    dist = str,
    source_date_epoch = int,
)

# TODO: Generalize the currently pinned build architecture.
_ARCH = "x86_64"

# buildifier: disable=name-conventions  (a type alias, conventionally UpperCamelCase)
PackageMetadata = dict[str, typing.Any]

def _build_requires(meta: SrcpkgMetadata) -> list[str]:
    """Combine common and architecture-specific BuildRequires."""
    brs = meta.build_requires
    return sorted(brs["_all"] + brs.get(_ARCH, []))

def _parse_metadata(meta: PackageMetadata) -> SrcpkgMetadata:
    """Validate one generated JSON value and return its typed representation."""
    data = dict(meta)
    data["sources"] = [SourceMetadata(**source) for source in data["sources"]]
    return SrcpkgMetadata(**data)

# buildifier: disable=unnamed-macro  (declares source http_files and one rpm_package)
def _declare_rpm_package(
        package: str,
        buildroot: str,
        meta: SrcpkgMetadata,
        buildroot_deps: list[str],
        rpmbuild_options: list[str] = []) -> None:
    spec = "{}/{}.spec".format(package, package)
    srcs = []
    for source in meta.sources:
        out = source.url.rsplit("/", 1)[-1]
        target = "{}--{}".format(package, out)
        native.http_file(
            name = target,
            out = out,
            urls = [source.url],
            sha256 = source.sha256sum,
            size_bytes = source.size,
        )
        srcs.append(":" + target)
    srcs += native.glob(["{}/*".format(package)], exclude = [spec])
    rpm_package(
        name = package,
        package = package,
        spec = spec,
        buildroot = buildroot,
        srcs = srcs,
        release = meta.release,
        dist = meta.dist,
        source_date_epoch = meta.source_date_epoch,
        subpackages = sorted(meta.binaries[_ARCH]),
        build_requires = _build_requires(meta),
        buildroot_deps = buildroot_deps,
        rpmbuild_options = rpmbuild_options,
    )

# buildifier: disable=unnamed-macro  (fan-out macro: an rpm_package + its source http_files)
# buildifier: disable=function-docstring-args
def rpm_package_json(
        package: str,
        buildroot: str,
        meta: PackageMetadata,
        buildroot_deps: list[str] = [],
        rpmbuild_options: list[str] = []) -> None:
    """Validate generated metadata and project it onto `rpm_package`."""
    _declare_rpm_package(
        package = package,
        buildroot = buildroot,
        meta = _parse_metadata(meta),
        buildroot_deps = buildroot_deps,
        rpmbuild_options = rpmbuild_options,
    )

def _cap(dep: str) -> str:
    """A BuildRequires/Provides capability with its version constraint stripped.

    'foo >= 1' -> 'foo'; 'pkgconfig(bar)' and path names pass through."""
    return dep.split(" ")[0]

# RPM rich-dependency and version operators.
_RICH_OPS = {op: True for op in ["and", "or", "if", "else", "unless", "with", "without"]}
_VERSION_OPS = {op: True for op in ["<", "<=", "=", ">=", ">"]}

def _br_caps(br: str) -> list[str]:
    """Extract capabilities from a plain or non-nested rich dependency."""
    if not br.startswith("("):
        return [_cap(br)]
    if not br.endswith(")"):
        fail("malformed rich dependency '{}'".format(br))
    caps = []
    skip = False  # the operand following a version operator
    for tok in br[1:-1].split():
        if skip:
            skip = False
        elif tok in _VERSION_OPS:
            skip = True
        elif tok not in _RICH_OPS:
            caps.append(_cap(tok))
    return caps

def _sccs(edges: dict[str, list[str]]) -> dict[str, int]:
    """Map nodes to strongly connected components using iterative Tarjan."""
    order = sorted(edges)
    steps = len(order)
    for v in order:
        steps += len(edges[v])
    index = {}  # node -> DFS index
    low = {}  # node -> lowlink
    comp = {}  # node -> SCC id
    onstack = {}
    tstack = []  # Tarjan's node stack
    idx = 0
    cid = 0
    for root in order:
        if root in index:
            continue
        call = [(root, 0)]  # explicit DFS stack of (node, next-edge-index)
        for _step in range(steps + 1):
            if not call:
                break
            v, ei = call[len(call) - 1]
            if ei == 0:
                index[v] = idx
                low[v] = idx
                idx += 1
                tstack.append(v)
                onstack[v] = True
            succ = edges[v]
            if ei < len(succ):
                w = succ[ei]
                call[len(call) - 1] = (v, ei + 1)
                if w not in index:
                    call.append((w, 0))
                elif onstack.get(w, False):
                    low[v] = min(low[v], index[w])
            else:
                if low[v] == index[v]:
                    for _pop in range(len(order)):
                        w = tstack.pop()
                        onstack[w] = False
                        comp[w] = cid
                        if w == v:
                            break
                    cid += 1
                call.pop()
                if call:
                    parent = call[len(call) - 1][0]
                    low[parent] = min(low[parent], low[v])
    return comp

def _provides(packages: dict[str, SrcpkgMetadata]) -> dict[str, dict[str, bool]]:
    """Map each provided capability (subpackage names, Provides, Files) to its providers."""
    provides = {}
    for name in sorted(packages):
        for arch_bins in packages[name].binaries.values():
            for binname in arch_bins:
                bm = arch_bins[binname]
                for cap in [binname] + bm["Provides"] + bm["Files"]:
                    provides.setdefault(_cap(cap), {})[name] = True
    return provides

def _buildrequires_edges(
        packages: dict[str, SrcpkgMetadata],
        provides: dict[str, dict[str, bool]],
        seed_only_packages: list[str]) -> dict[str, dict[str, list[str]]]:
    """The package-to-self-hosted-provider graph, with the capabilities justifying each edge.

    A seed-only source package (tool use rather than linkage; see docs/self-host-approaches.md)
    contributes no edges as a provider: BuildRequires on it always resolve from the seed."""
    edges = {}
    for name in sorted(packages):
        deps = {}
        for br in _build_requires(packages[name]):
            for cap in _br_caps(br):
                for p in provides.get(cap, {}):
                    if p != name and p not in seed_only_packages:
                        deps.setdefault(p, {})[cap] = True
        edges[name] = {p: sorted(deps[p]) for p in sorted(deps)}
    return edges

def _buildroot_locks(
        edge_caps: dict[str, dict[str, list[str]]],
        provides: dict[str, dict[str, bool]],
        buildroot_only_packages: list[str]) -> dict[str, list[str]]:
    """Build an acyclic package-to-self-hosted-provider map.

    Cyclic edges fall back to upstream unless a buildroot-only package requires them.
    The retained graph must still be acyclic."""
    for cap in buildroot_only_packages:
        if cap not in provides:
            fail("buildroot-only package '{}' has no provider among the branch packages".format(cap))
    comp = _sccs({name: sorted(deps) for name, deps in edge_caps.items()})
    locks = {}
    for name in edge_caps:
        keep = []
        for p in edge_caps[name]:
            kept = [cap for cap in edge_caps[name][p] if cap in buildroot_only_packages]
            if comp[p] != comp[name] or kept:
                keep.append(p)
        locks[name] = keep
    lockcomp = _sccs(locks)
    members = {}  # SCC id -> member count; any id shared by two nodes is a cycle
    for name in locks:
        members[lockcomp[name]] = members.get(lockcomp[name], 0) + 1
    for name in sorted(locks):
        if members[lockcomp[name]] > 1:
            fail("buildroot-only packages reintroduce a BuildRequires cycle through '{}'".format(name))
    return locks

def _buildrequires_graph_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.write_json(
        "buildrequires-graph.json",
        {"edges": ctx.attrs.edges, "sccs": ctx.attrs.sccs},
        pretty = True,
    )
    return [DefaultInfo(default_output = out)]

# The branch's raw BuildRequires graph as JSON, for `buck run tine//tools:scc` (cycle analysis).
_buildrequires_graph = rule(
    impl = _buildrequires_graph_impl,
    attrs = {
        # requirer -> provider -> the BuildRequires capabilities justifying the edge
        "edges": attrs.dict(attrs.string(), attrs.dict(attrs.string(), attrs.list(attrs.string()))),
        # package -> strongly-connected-component id (a shared id marks a cycle)
        "sccs": attrs.dict(attrs.string(), attrs.int()),
    },
)

# buildifier: disable=unnamed-macro  (fan-out macro: an rpm_package_json per branch package)
def rpm_branch(
        buildroot: str,
        packages: dict[str, PackageMetadata],
        buildroot_only_packages: list[str] = [],
        seed_only_packages: list[str] = [],
        rpmbuild_options: dict[str, list[str]] = {}) -> None:
    """Declare a branch and its self-hosting buildroot edges."""
    metadata = {name: _parse_metadata(meta) for name, meta in packages.items()}
    for pin in seed_only_packages:
        if pin not in metadata:
            fail("seed-only package '{}' is not among the branch packages".format(pin))
    provides = _provides(metadata)
    for cap in buildroot_only_packages:
        for p in provides.get(cap, {}):
            if p in seed_only_packages:
                fail("buildroot-only package '{}' is provided by seed-only package '{}'".format(cap, p))
    edge_caps = _buildrequires_edges(metadata, provides, seed_only_packages)
    locks = _buildroot_locks(edge_caps, provides, buildroot_only_packages)

    # An underscore never starts a valid rpm name, so this cannot clash with a package target.
    _buildrequires_graph(
        name = "_buildrequires_graph",
        edges = edge_caps,
        sccs = _sccs({name: sorted(deps) for name, deps in edge_caps.items()}),
    )
    for name in sorted(metadata):
        _declare_rpm_package(
            package = name,
            buildroot = buildroot,
            meta = metadata[name],
            buildroot_deps = [":" + dep for dep in locks[name]],
            rpmbuild_options = rpmbuild_options.get(name, []),
        )
