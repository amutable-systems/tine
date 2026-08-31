#!/usr/bin/python3
"""Find a Cargo workspace and re-encode its lock for a dynamic action."""

import json
import tomllib
from pathlib import Path
from typing import Any, TypedDict

import specs

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
    sources: dict[str, str]


def _packages(target: str, lock: dict[str, Any]) -> list[dict[str, Any]]:
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise SystemExit(f"cargo_package {target}: `lock` is not a Cargo.lock: it has no [[package]] list")
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
            raise SystemExit(
                f"cargo_package {target}: {name} {version}: unsupported dependency source {source}"
            )
        if "checksum" not in package:
            raise SystemExit(
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
            raise SystemExit(
                f"cargo_package {target}: git source without a full commit in the lock: {source}"
            )
        url, _, query = head.removeprefix("git+").partition("?")
        fields = {"git": url}
        for parameter in query.split("&"):
            name, assignment, value = parameter.partition("=")
            if assignment and name in _GIT_REFERENCES:
                fields[name] = value
        previous = sources.setdefault(commit, fields)
        if previous != fields:
            raise SystemExit(
                f"cargo_package {target}: commit {commit[:12]} comes from two spellings of one git "
                f"source ({previous} vs {fields}); make the dependency declarations agree"
            )
    return sources


def _named(sources: dict[str, str], name: str) -> list[tuple[Path, Path]]:
    """Find logical and materialized paths named `name`, including inside directory artifacts."""
    found: dict[Path, Path] = {}
    for short_path, artifact_path in sorted(sources.items()):
        logical = Path(short_path)
        artifact = Path(artifact_path)
        if artifact.is_dir():
            for directory, directories, files in artifact.walk():
                directories.sort()
                if name in files:
                    path = directory / name
                    found[logical / path.relative_to(artifact)] = path
        elif logical.name == name:
            found[logical] = artifact
    return sorted(found.items())


def resolve_workspace(target: str, sources: dict[str, str]) -> dict[str, Any]:
    """Return the workspace root and remote inputs resolved from its lock."""
    locks = _named(sources, "Cargo.lock")
    if len(locks) > 1:
        raise SystemExit(
            f"cargo_package {target}: srcs hold several Cargo.lock files: "
            f"{[str(logical) for logical, _ in locks]}"
        )

    manifests = _named(sources, "Cargo.toml")
    if locks:
        root = locks[0][0].parent
        manifests = [manifest for manifest in manifests if manifest[0].parent == root]
    elif not manifests:
        raise SystemExit(
            f"cargo_package {target}: srcs hold no Cargo.toml; by default the checkout is expected "
            f"in the {target}/ directory, pass `srcs` when it lives elsewhere"
        )
    elif len(manifests) == 1:
        root = manifests[0][0].parent
    else:
        raise SystemExit(
            f"cargo_package {target}: srcs hold no Cargo.lock and {len(manifests)} Cargo.toml files; "
            "commit the lock"
        )

    if not manifests:
        raise SystemExit(f"cargo_package {target}: srcs hold no Cargo.toml beside the Cargo.lock")

    lock: dict[str, Any] = {"package": []}
    if locks:
        lock = tomllib.loads(locks[0][1].read_text(encoding="utf-8"))
    root_string = "" if root == Path(".") else str(root)
    return {
        "crates": crate_downloads(target, lock),
        "git": git_sources(target, lock),
        "root": root_string,
    }


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "cargo-lock", argv)
    workspace = resolve_workspace(spec["name"], spec["sources"])
    Path(spec["out"]).write_text(json.dumps(workspace, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
