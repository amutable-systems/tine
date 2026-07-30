"""Read a load()-ed Cargo.lock: the crate downloads a project's build needs.

Cargo pins every dependency's exact version and the sha256 of its registry tarball, and crates.io
serves those tarballs under a derivable URL. The lock therefore already describes every download a
build needs, with no network access to work out what.

The lock is loaded in the consuming BUCK file rather than read by an action: the downloads have to
exist before anything runs, and the load makes the committed file itself a tracked input of the
parse.

Deliberately kept to the subset of Starlark that is also plain Python, so the unit suite can
exercise these functions directly (tests/test_cargo.py runs this file through exec()).
"""

# Both spellings of the crates.io index. The sparse protocol replaced the git one, and locks written
# before a project switched over keep the old string.
_CRATES_IO = [
    "registry+https://github.com/rust-lang/crates.io-index",
    "sparse+https://index.crates.io/",
]

def _packages(target: str, lock: dict[str, typing.Any]) -> list[dict[str, typing.Any]]:
    packages = lock.get("package")
    if type(packages) != type([]):
        fail("cargo_package {}: `lock` is not a load()-ed Cargo.lock: it has no [[package]] list".format(target))
    return packages

def crate_downloads(target: str, lock: dict[str, typing.Any]) -> list[dict[str, str]]:
    """One hash-pinned download per registry crate the lock names, ordered by name and version.

    Packages without a source are the workspace's own members, which arrive with the source tree.
    """
    crates = []
    for package in _packages(target, lock):
        name, version = package["name"], package["version"]
        source = package.get("source")
        if source == None:
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
