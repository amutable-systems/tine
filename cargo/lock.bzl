"""Read a resolved Cargo.lock: everything a project's build must fetch.

Cargo pins every dependency's exact version and the sha256 of its registry tarball, and crates.io
serves those tarballs under a derivable URL. A git dependency is pinned by the commit in its lock
source. The lock therefore already describes every fetch a build needs, with no network access to
work out what.

A dynamic action reads the lock once it has been built and declares those fetches then, so the lock
is an ordinary input rather than something the consuming BUCK file has to load: a project whose
tree arrives from a fetch pins its build the same way a checked-out one does.

Deliberately kept to the subset of Starlark that is also plain Python, so the unit suite can
exercise these functions directly (cargo_test.py runs this file through exec()).
"""

# Both spellings of the crates.io index. The sparse protocol replaced the git one, and locks written
# before a project switched over keep the old string.
_CRATES_IO = [
    "registry+https://github.com/rust-lang/crates.io-index",
    "sparse+https://index.crates.io/",
]

# The references cargo accepts on a git source.
_GIT_REFERENCES = ["branch", "tag", "rev"]

def _packages(target: str, lock: dict[str, typing.Any]) -> list[dict[str, typing.Any]]:
    packages = lock.get("package")
    if type(packages) != type([]):
        fail("cargo_package {}: `lock` is not a Cargo.lock: it has no [[package]] list".format(target))
    return packages

def crate_downloads(target: str, lock: dict[str, typing.Any]) -> list[dict[str, str]]:
    """One hash-pinned download per registry crate the lock names, ordered by name and version.

    Packages without a source are the workspace's own members, which arrive with the source tree,
    and a git dependency arrives as a repository fetch instead (see git_sources).
    """
    crates = []
    for package in _packages(target, lock):
        name, version = package["name"], package["version"]
        source = package.get("source")
        if source == None or source.startswith("git+"):
            continue
        if source not in _CRATES_IO:
            fail(
                "cargo_package {}: {} {}: unsupported dependency source {}".format(
                    target,
                    name,
                    version,
                    source,
                )
            )
        if "checksum" not in package:
            fail(
                "cargo_package {}: {} {}: no checksum; Cargo.lock version 3 or newer is required".format(
                    target,
                    name,
                    version,
                )
            )
        crates.append({
            "name": name,
            "sha256": package["checksum"],
            "url": "https://static.crates.io/crates/{}/{}-{}.crate".format(name, name, version),
            "version": version,
        })
    return sorted(crates, key = lambda crate: (crate["name"], crate["version"]))

def git_sources(target: str, lock: dict[str, typing.Any]) -> dict[str, dict[str, str]]:
    """The distinct git sources the lock names: commit -> the fields cargo identifies the source by.

    A source reads `git+<url>[?<reference>]#<commit>`: the fragment is the commit cargo resolved,
    and the query carries the reference the project asked for; a dependency tracking the default
    branch names no reference at all. Cargo matches a source in config.toml by these fields, not by
    any particular spelling of them, and pins a git dependency by the commit alone, transitive
    dependencies included, so nothing else has to be recorded.
    """
    sources = {}
    for package in _packages(target, lock):
        source = package.get("source", "")
        if not source.startswith("git+"):
            continue
        head, separator, commit = source.rpartition("#")
        if not separator or len(commit) != 40 or commit.lower().strip("0123456789abcdef"):
            fail("cargo_package {}: git source without a full commit in the lock: {}".format(target, source))
        url, _, query = head.removeprefix("git+").partition("?")
        fields = {"git": url}
        for parameter in query.split("&"):
            name, assignment, value = parameter.partition("=")
            if assignment and name in _GIT_REFERENCES:
                fields[name] = value
        # Two spellings of one dependency would need two replacement stanzas; without them cargo
        # fails offline, far from the cause.
        previous = sources.setdefault(commit, fields)
        if previous != fields:
            fail(
                "cargo_package {}: commit {} comes from two spellings of one git source ({} vs {}); make the dependency declarations agree".format(
                    target,
                    commit[:12],
                    previous,
                    fields,
                )
            )
    return sources
