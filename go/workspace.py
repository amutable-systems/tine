#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

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
from util import fail

_MODULE = "go.mod"
_SUM = "go.sum"
_WORK = "go.work"


class Spec(TypedDict):
    # The target, named in whatever this refuses.
    name: str
    # Where to write the resolved workspace.
    out: str
    # The project's source directory artifact.
    src: str


def _named(source: Path, name: str) -> list[Path]:
    """Find files called `name`, relative to the source directory."""
    found: list[Path] = []
    for directory, _, files in source.walk():
        if name in files:
            found.append((directory / name).relative_to(source))
    return sorted(found)


def resolve_workspace(target: str, source: Path) -> dict[str, str | None]:
    """Find the module root and pins relative to the source directory."""
    if not source.is_dir():
        fail(f"go_package {target}: src must be a directory")
    if _named(source, _WORK):
        fail(f"go_package {target}: go workspaces are not supported; keep {_WORK} out of src")

    modules = _named(source, _MODULE)
    if not modules:
        fail(
            f"go_package {target}: src holds no {_MODULE}; by default the checkout is expected in "
            f"the {target}/ directory, pass `src` when it lives elsewhere"
        )

    # A second go.mod belongs to a module nested in the project, a tools or testdata helper, common
    # enough in Go repositories. go leaves those out of a `./...` build by itself, so the one
    # containing all the others is the project's own.
    module = min(modules, key=lambda path: len(path.parts))
    root = module.parent
    strays = [str(other) for other in modules if other != module and root not in other.parents]
    if strays:
        fail(
            f"go_package {target}: {strays} is not nested in {module}, so src holds no single "
            "project; narrow `src` to one module"
        )

    # A module that resolves nothing has nothing to pin, and then nothing is fetched either.
    pinned = root / _SUM
    return {
        "mod": str(module),
        "root": "" if root == Path() else str(root),
        "sum": str(pinned) if pinned in _named(source, _SUM) else None,
    }


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "go-workspace", argv)
    workspace = resolve_workspace(spec["name"], Path(spec["src"]))
    Path(spec["out"]).write_text(json.dumps(workspace, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
