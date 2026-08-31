"""Bootstrap a root from a package system's own archives, before its package manager exists.

Every package system's bootstrap extractor answers the same request: a set of packages, or
directories of them, and a root to lay them down in. Which archive format that means opening is
the only part that differs, so the expansion, the ordering, the capture and the invocation happen
here and a driver is left with one package at a time.
"""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import specs

import rootfs


class Spec(TypedDict):
    out: str
    # Packages, or directories of them.
    packages: list[str]


def expand(paths: list[str]) -> list[Path]:
    """Expand any directory argument to the (sorted) files it contains."""
    found: list[Path] = []
    for path in (Path(package) for package in paths):
        found += sorted(path.iterdir()) if path.is_dir() else [path]
    return found


def run(prog: str, unpack: Callable[[Path, Path], int], argv: list[str] | None = None) -> None:
    """Unpack everything this invocation names into the root it names.

    The callback receives one package and the destination, and returns how many entries it wrote.
    """
    spec = specs.parse(Spec, prog, argv)
    dest = Path(spec["out"])
    packages = expand(spec["packages"])
    if not packages:
        raise SystemExit(f"{prog}: no packages to extract")
    with rootfs.capture_on_exit(dest):
        total = sum(unpack(package, dest) for package in packages)
    print(f"extracted {total} entries from {len(packages)} package(s) into {dest}", file=sys.stderr)
