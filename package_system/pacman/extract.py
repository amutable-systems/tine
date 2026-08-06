"""Bootstrap a box by unpacking alpm packages without pacman.

An alpm package is an ordinary compressed tar of the tree it installs, so the bootstrap needs
no package tooling and no separate payload representation. Its metadata and install scriptlets
are deferred to the real install that follows.
"""

import sys
from contextlib import ExitStack
from pathlib import Path
from typing import TypedDict

import specs

import alpm
import rootfs


class Spec(TypedDict):
    out: str
    # Packages, or directories of them.
    packages: list[str]


def extract(package: Path, dest: Path) -> int:
    """Extract one package's tree into dest and return the number of entries written."""
    written = 0
    with ExitStack() as stack:
        archive = alpm.open_package(stack, package)
        for member in archive:
            # alpm reserves every top-level dot entry for its own metadata, so none are files
            # the package installs. Their contents belong to the real install that follows.
            if member.name.startswith("."):
                continue
            # The `tar` filter keeps modes and links but refuses to escape the destination.
            archive.extract(member, dest, filter="tar")
            written += 1
    return written


def _expand(paths: list[Path]) -> list[Path]:
    """Expand any directory argument to the (sorted) files it contains."""
    out: list[Path] = []
    for path in paths:
        out += sorted(path.iterdir()) if path.is_dir() else [path]
    return out


def main(argv: list[str] | None = None) -> None:
    spec: Spec = specs.parse("extract", argv)
    dest = Path(spec["out"])
    packages = _expand([Path(package) for package in spec["packages"]])
    if not packages:
        raise SystemExit("extract: no packages to extract")
    total = sum(extract(package, dest) for package in packages)
    rootfs.capture(dest)
    print(f"extracted {total} entries from {len(packages)} package(s) into {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
