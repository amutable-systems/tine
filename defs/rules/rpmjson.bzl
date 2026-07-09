"""rpm_package_json: build an rpm from generated <package>.json metadata.

This is the only module that knows the JSON schema, as the hand-over point
between the package importer and buck build system.

The generated per-distro/release BUCK loads each <package>.json and hands them all to
`rpm_branch`, which computes the self-hosting buildroot lock across the branch and projects
each onto `rpm_package_json`; `SrcpkgMetadata(**meta)` validates each against the schema at
load time (missing/extra/mistyped fields fail the parse).

"""

load("@prelude//:native.bzl", "native")
load(":rpm.bzl", "rpm_package")

# Mirrors importer's SrcpkgMetadata TypedDict (the generated <package>.json), keep in sync.
# buildifier: disable=name-conventions  (a record *type*, conventionally UpperCamelCase)
SrcpkgMetadata = record(
    build_requires = dict[str, list[str]],  # "_all" + per-arch conditional extras
    # producing build arch → pkgname → {Files, Requires, Recommends, Provides}
    binaries = dict,
    sources = list[dict[str, str]],
    version = str,  # main-package version; recorded metadata, not consumed by the build (spec drives it)
    release = str,
    dist = str,
    source_date_epoch = int,
)

# TODO: The whole stack resolves + builds x86_64 only for now (cf. plan.py's pinned arch), so
# select that slice of the per-arch static BuildRequires.
_ARCH = "x86_64"

def _build_requires(meta: dict) -> list[str]:
    """A package's effective BuildRequires: the arch-common set + the pinned arch's extras.

    An arch with no conditional extras has no key."""
    brs = meta["build_requires"]
    return sorted(brs["_all"] + brs.get(_ARCH, []))

# buildifier: disable=unnamed-macro  (fan-out macro: an rpm_package + its source http_files)
def rpm_package_json(package: str, distribution: str, meta: dict, buildroot_deps: list[str] = []) -> None:
    """Project a generated <package>.json onto rpm_package.

    `meta` is the natively-decoded json dict, matching SrcpkgMetadata (validated on load).
    The spec + committed Source/Patch files live in the package's <package>/ dist-git subdir;
    each upstream `sources` entry becomes an http_file dropped into SOURCES under its URL
    basename (http_file needs the SHA256; the lookaside itself is keyed by SHA512).

    `buildroot_deps` (from rpm_branch's per-branch lock) are sibling package labels whose builds
    provide some of this package's BuildRequires; they overlay the buildroot so we build against
    our own rpms, not Fedora's. Empty for packages with no self-hosted BR.
    """

    # fails on schema mismatch
    m = SrcpkgMetadata(**meta)
    spec = "{}/{}.spec".format(package, package)
    srcs = []
    for s in m.sources:
        out = s["url"].rsplit("/", 1)[-1]
        target = "{}--{}".format(package, out)
        native.http_file(name = target, out = out, urls = [s["url"]], sha256 = s["sha256sum"])
        srcs.append(":" + target)
    srcs += native.glob(["{}/*".format(package)], exclude = [spec])
    rpm_package(
        name = package,
        package = package,
        spec = spec,
        distribution = distribution,
        srcs = srcs,
        release = m.release,
        dist = m.dist,
        source_date_epoch = m.source_date_epoch,
        # drives the sub-targets + fidelity gate
        subpackages = sorted(m.binaries[_ARCH]),
        build_requires = _build_requires(meta),
        buildroot_deps = buildroot_deps,
    )

def _cap(dep: str) -> str:
    """A BuildRequires/Provides capability with its version constraint stripped.

    'foo >= 1' -> 'foo'; 'pkgconfig(bar)' and path names pass through."""
    return dep.split(" ")[0]

# rpm rich-dependency boolean operators (`(glibc32 or glibc-devel(x86-32))` and friends)
# and the version-constraint comparison operators.
_RICH_OPS = {op: True for op in ["and", "or", "if", "else", "unless", "with", "without"]}
_VERSION_OPS = {op: True for op in ["<", "<=", "=", ">=", ">"]}

def _br_caps(br: str) -> list[str]:
    """The capabilities a BuildRequires string references.

    A plain dependency yields its _cap. A rich dependency `(a or b ...)` sheds the outer parens
    and yields each token's _cap, dropping the boolean operators and each version operator with
    its operand. Nested parens aren't parsed, they are part of the cap name (usually "Provides").
    """
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
    """Map each node to a strongly-connected-component id (iterative Tarjan).

    Starlark has no recursion, hence the explicit stack. Nodes sharing an id form a
    BuildRequires cycle; buildroot edges within one are dropped (see _buildroot_locks).
    `edges` is node -> list of successor nodes."""
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
    """Per-package self-hosting buildroot deps: package -> sibling packages providing its BRs.

    The revdep map read forward: provides = binary names ∪ Provides ∪ Files (paths are a kind of
    Provides), and X build-depends on P iff X's BuildRequires intersect P's provides. Condensed to
    a DAG — edges inside a BuildRequires cycle are dropped (those caps fall back to the upstream
    seed) — since buck rejects cyclic deps. Scoped to `packages`.

    Edges justified `buildroot_only_packages` are exempt from dropping: the seed cannot substitute
    for those (koji satisfies them from buildroot-only packages the compose never ships). The kept
    edges are re-checked to still form a DAG.
    """
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
def rpm_branch(distribution: str, packages: dict, buildroot_only_packages: list[str] = []) -> None:
    """Declare every package in a distro/release branch, self-hosting lock included.

    Computes the self-hosting buildroot lock across the whole branch (see _buildroot_locks) and
    projects each package onto rpm_package_json. The importer emits only the data (this `packages`
    map of name -> loaded <package>.json); all build/resolution logic lives here in buck.

    `buildroot_only_packages` are packages that exist only in the distribution's build ecosystem,
    never in its compose (koji's build-only packages, primarily "glibc32"): the lock keeps a
    BuildRequires edge for these even inside a cycle, because the seed cannot substitute for
    it.
    """
    locks = _buildroot_locks(packages, buildroot_only_packages)
    for name in sorted(packages):
        rpm_package_json(
            package = name,
            distribution = distribution,
            meta = packages[name],
            # package name → package-relative buck target label
            buildroot_deps = [":" + dep for dep in locks[name]],
        )
