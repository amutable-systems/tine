#!/usr/bin/python3
"""Find a Cargo workspace and re-encode its lock for a dynamic action."""

import json
import tomllib
from pathlib import Path
from typing import Any, TypedDict

import specs
from util import fail, named_files

# Both spellings of the crates.io index. The sparse protocol replaced the git one, and locks written
# before a project switched over keep the old string.
_CRATES_IO = (
    "registry+https://github.com/rust-lang/crates.io-index",
    "sparse+https://index.crates.io/",
)

# The references cargo accepts on a git source.
_GIT_REFERENCES = ("branch", "tag", "rev")


class Spec(TypedDict):
    name: str
    out: str
    src: str


def _packages(target: str, lock: dict[str, Any]) -> list[dict[str, Any]]:
    packages = lock.get("package")
    if not isinstance(packages, list):
        fail(f"cargo_package {target}: `lock` is not a Cargo.lock: it has no [[package]] list")
    return packages


def crate_downloads(target: str, lock: dict[str, Any]) -> list[dict[str, str]]:
    """Return one hash-pinned download per registry crate named by the lock."""
    crates = []
    for package in _packages(target, lock):
        name, version = package["name"], package["version"]
        source = package.get("source")
        if source is None or source.startswith("git+"):
            continue
        if source not in _CRATES_IO:
            fail(f"cargo_package {target}: {name} {version}: unsupported dependency source {source}")
        if "checksum" not in package:
            fail(
                f"cargo_package {target}: {name} {version}: no checksum; "
                "Cargo.lock version 3 or newer is required"
            )
        crates.append(
            {
                "name": name,
                "sha256": package["checksum"],
                "url": f"https://static.crates.io/crates/{name}/{name}-{version}.crate",
                "version": version,
            }
        )
    return sorted(crates, key=lambda crate: (crate["name"], crate["version"]))


def git_sources(target: str, lock: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Return the distinct git sources named by the lock, keyed by resolved commit."""
    sources: dict[str, dict[str, str]] = {}
    for package in _packages(target, lock):
        source = package.get("source", "")
        if not source.startswith("git+"):
            continue
        head, separator, commit = source.rpartition("#")
        if not separator or len(commit) != 40 or commit.lower().strip("0123456789abcdef"):
            fail(f"cargo_package {target}: git source without a full commit in the lock: {source}")
        url, _, query = head.removeprefix("git+").partition("?")
        fields = {"git": url}
        for parameter in query.split("&"):
            name, assignment, value = parameter.partition("=")
            if assignment and name in _GIT_REFERENCES:
                fields[name] = value
        previous = sources.setdefault(commit, fields)
        if previous != fields:
            fail(
                f"cargo_package {target}: commit {commit[:12]} comes from two spellings of one git "
                f"source ({previous} vs {fields}); make the dependency declarations agree"
            )
    return sources


def resolve_workspace(target: str, source: Path) -> dict[str, Any]:
    """Return the workspace root and remote inputs resolved from its lock."""
    if not source.is_dir():
        fail(f"cargo_package {target}: src must be a directory")
    locks = named_files(source, "Cargo.lock")
    if locks:
        # Cargo only ever reads the lock at the workspace root, so one below the outermost, a vendored
        # project's say, cannot be the workspace. A live override and the pinned fetch must agree here.
        outermost = min(locks, key=lambda path: len(path.parts))
        if all(outermost.parent in other.parents for other in locks if other != outermost):
            locks = [outermost]
    if len(locks) > 1:
        fail(f"cargo_package {target}: src holds several Cargo.lock files: {[str(path) for path in locks]}")

    manifests = named_files(source, "Cargo.toml")
    if locks:
        root = locks[0].parent
        manifests = [manifest for manifest in manifests if manifest.parent == root]
    elif not manifests:
        fail(
            f"cargo_package {target}: src holds no Cargo.toml; by default the checkout is expected "
            f"in the {target}/ directory, pass `src` when it lives elsewhere"
        )
    elif len(manifests) == 1:
        root = manifests[0].parent
    else:
        fail(
            f"cargo_package {target}: src holds no Cargo.lock and {len(manifests)} Cargo.toml files; "
            "commit the lock"
        )

    if not manifests:
        fail(f"cargo_package {target}: src holds no Cargo.toml beside the Cargo.lock")

    lock: dict[str, Any] = {"package": []}
    if locks:
        lock = tomllib.loads((source / locks[0]).read_text(encoding="utf-8"))
    root_string = "" if root == Path(".") else str(root)
    return {
        "crates": crate_downloads(target, lock),
        "git": git_sources(target, lock),
        "root": root_string,
    }


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "cargo-lock", argv)
    workspace = resolve_workspace(spec["name"], Path(spec["src"]))
    Path(spec["out"]).write_text(json.dumps(workspace, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
