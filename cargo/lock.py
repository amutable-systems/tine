#!/usr/bin/python3
"""Find a Cargo workspace and re-encode its lock for a dynamic action."""

import json
import tomllib
from pathlib import Path
from typing import Any, TypedDict

import specs


class Spec(TypedDict):
    name: str
    out: str
    sources: dict[str, str]


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
    """Return the workspace root and parsed lock represented by `sources`."""
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
    return {"lock": lock, "root": root_string}


def main(argv: list[str] | None = None) -> None:
    spec: Spec = specs.parse("cargo-lock", argv)
    workspace = resolve_workspace(spec["name"], spec["sources"])
    Path(spec["out"]).write_text(json.dumps(workspace, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
