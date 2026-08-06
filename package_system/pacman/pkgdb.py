#!/usr/bin/python3
"""Copy the alpm database out of a logical image as a separate, trimmed artifact.

Useful as a basis for SBOM creation and security scanners. The database is not shipped in
the image, so capture it as a separate artifact. The output is the directory a package
database occupies; alpm's is a directory of per-package entries, copied here as it stands.

Each entry's `mtree` is dropped: it is a manifest of the files the package shipped, which
pacman alone reads to verify an installed tree, and it is the largest part of an entry after
the file list. What identifies the package (`desc`) and what it owns (`files`) both stay.
"""

import shutil
import sys
from pathlib import Path

import specs

import alpm
import finalize


class Spec(finalize.ImageSpec):
    out: str


def main(argv: list[str] | None = None) -> None:
    spec: Spec = specs.parse("pkgdb", argv)

    out = Path(spec["out"])
    out.mkdir(parents=True, exist_ok=True)
    with finalize.image(spec, program="pkgdb") as tree:
        source = tree / alpm.LOCAL_DB
        if not source.is_dir():
            raise SystemExit(f"no alpm database at {source}; the image has no installed packages")
        shutil.copytree(
            source,
            out,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("mtree"),
        )
    entries = sorted(out.glob("*/desc"))
    if not entries:
        raise SystemExit(f"no alpm database entries under {source}")
    print(f"pkgdb: captured {len(entries)} package entries -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
