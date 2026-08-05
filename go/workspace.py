#!/usr/bin/python3
"""Find the Go module a project's sources hold, ahead of the actions that build it.

A project is checked out, not written by us, so it carries no build file pointing at its own root;
the go.mod marks it. A tree that arrives as a directory artifact only says where that sits once it
has been built, so this runs as an action and the fetch and the build are declared from what it
reports.
"""

import json
from pathlib import Path
from typing import TypedDict

import specs

_MODULE = "go.mod"
_SUM = "go.sum"
_WORK = "go.work"


class Spec(TypedDict):
    # The target, named in whatever this refuses.
    name: str
    # Where to write the resolved workspace.
    out: str
    # The project's sources: logical path -> materialized path, directory artifacts included.
    sources: dict[str, str]


def _named(sources: dict[str, str], name: str) -> list[Path]:
    """The logical path of every file called `name`, those inside directory artifacts included."""
    found: list[Path] = []
    for short_path, artifact_path in sorted(sources.items()):
        logical, artifact = Path(short_path), Path(artifact_path)
        if artifact.is_dir():
            for directory, directories, files in artifact.walk():
                directories.sort()
                if name in files:
                    found.append(logical / (directory / name).relative_to(artifact))
        elif logical.name == name:
            found.append(logical)
    return sorted(found)


def resolve_workspace(target: str, sources: dict[str, str]) -> dict[str, str | None]:
    """The project's go.mod, its go.sum if it has one, and the module root, as logical paths."""
    if _named(sources, _WORK):
        raise SystemExit(f"go_package {target}: go workspaces are not supported; keep {_WORK} out of srcs")

    modules = _named(sources, _MODULE)
    if not modules:
        raise SystemExit(
            f"go_package {target}: srcs hold no {_MODULE}; by default the checkout is expected in "
            f"the {target}/ directory, pass `srcs` when it lives elsewhere"
        )

    # A second go.mod belongs to a module nested in the project, a tools or testdata helper, common
    # enough in Go repositories. go leaves those out of a `./...` build by itself, so the one
    # containing all the others is the project's own.
    module = min(modules, key=lambda path: len(path.parts))
    root = module.parent
    strays = [str(other) for other in modules if other != module and root not in other.parents]
    if strays:
        raise SystemExit(
            f"go_package {target}: {strays} is not nested in {module}, so srcs hold no single "
            "project; narrow `srcs` to one module"
        )

    # A module that resolves nothing has nothing to pin, and then nothing is fetched either.
    pinned = root / _SUM
    return {
        "mod": str(module),
        "root": "" if root == Path() else str(root),
        "sum": str(pinned) if pinned in _named(sources, _SUM) else None,
    }


def main(argv: list[str] | None = None) -> None:
    spec: Spec = specs.parse("go-workspace", argv)
    workspace = resolve_workspace(spec["name"], spec["sources"])
    Path(spec["out"]).write_text(json.dumps(workspace, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
