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
    binaries = dict,
    sources = list[SourceMetadata],
    version = str,  # main-package version; recorded metadata, not consumed by the build (spec drives it)
    release = str,
    dist = str,
    source_date_epoch = int,
)

# TODO: Generalize the currently pinned build architecture.
_ARCH = "x86_64"

def _build_requires(meta: dict) -> list[str]:
    """Combine common and architecture-specific BuildRequires."""
    brs = meta["build_requires"]
    return sorted(brs["_all"] + brs.get(_ARCH, []))

# buildifier: disable=unnamed-macro  (fan-out macro: an rpm_package + its source http_files)
# buildifier: disable=function-docstring-args
def rpm_package_json(package: str, buildroot: str, meta: dict, buildroot_deps: list[str] = [], rpm_macros: dict[str, str] = {}) -> None:
    """Validate generated metadata and project it onto `rpm_package`."""

    # Convert nested source dictionaries before validating the outer record.
    meta = dict(meta)
    meta["sources"] = [SourceMetadata(**s) for s in meta["sources"]]
    m = SrcpkgMetadata(**meta)  # fails on schema mismatch
    spec = "{}/{}.spec".format(package, package)
    srcs = []
    for s in m.sources:
        out = s.url.rsplit("/", 1)[-1]
        target = "{}--{}".format(package, out)
        native.http_file(name = target, out = out, urls = [s.url], sha256 = s.sha256sum, size_bytes = s.size)
        srcs.append(":" + target)
    srcs += native.glob(["{}/*".format(package)], exclude = [spec])
    rpm_package(
        name = package,
        package = package,
        spec = spec,
        buildroot = buildroot,
        srcs = srcs,
        release = m.release,
        dist = m.dist,
        source_date_epoch = m.source_date_epoch,
        subpackages = sorted(m.binaries[_ARCH]),
        build_requires = _build_requires(meta),
        buildroot_deps = buildroot_deps,
        macros = rpm_macros,
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

def _sccs(edges: dict) -> dict:
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

def _buildroot_locks(packages: dict, buildroot_only_packages: list[str]) -> dict:
    """Build an acyclic package-to-self-hosted-provider map.

    Cyclic edges fall back to upstream unless a buildroot-only package requires them.
    The retained graph must still be acyclic."""
    provides = {}  # capability -> {package: True}
    for name in sorted(packages):
        for arch_bins in packages[name]["binaries"].values():
            for binname in arch_bins:
                bm = arch_bins[binname]
                for cap in [binname] + bm["Provides"] + bm["Files"]:
                    provides.setdefault(_cap(cap), {})[name] = True
    for cap in buildroot_only_packages:
        if cap not in provides:
            fail("buildroot-only package '{}' has no provider among the branch packages".format(cap))
    edges = {}
    kept = {}  # package -> {provider: True} edges exempt from cycle dropping
    for name in sorted(packages):
        deps = {}
        keep = {}
        for br in _build_requires(packages[name]):
            for cap in _br_caps(br):
                for p in provides.get(cap, {}):
                    if p != name:
                        deps[p] = True
                        if cap in buildroot_only_packages:
                            keep[p] = True
        edges[name] = sorted(deps)
        kept[name] = keep
    comp = _sccs(edges)
    locks = {
        name: [p for p in edges[name] if comp[p] != comp[name] or kept[name].get(p, False)]
        for name in edges
    }
    lockcomp = _sccs(locks)
    members = {}  # SCC id -> member count; any id shared by two nodes is a cycle
    for name in locks:
        members[lockcomp[name]] = members.get(lockcomp[name], 0) + 1
    for name in sorted(locks):
        if members[lockcomp[name]] > 1:
            fail("buildroot-only packages reintroduce a BuildRequires cycle through '{}'".format(name))
    return locks

# buildifier: disable=unnamed-macro  (fan-out macro: an rpm_package_json per branch package)
def rpm_branch(buildroot: str, packages: dict, buildroot_only_packages: list[str] = [], rpm_macros: dict[str, dict[str, str]] = {}) -> None:
    """Declare a branch and its self-hosting buildroot edges."""
    locks = _buildroot_locks(packages, buildroot_only_packages)
    for name in sorted(packages):
        rpm_package_json(
            package = name,
            buildroot = buildroot,
            meta = packages[name],
            buildroot_deps = [":" + dep for dep in locks[name]],
            rpm_macros = rpm_macros.get(name, {}),
        )
