#!/usr/bin/python3
"""Copy the rpm database out of a logical image as a separate, trimmed artifact.

Useful as a basis for SBOM creation and security scanners. The database is not shipped in
the image, so capture it as a separate artifact.

The copy is trimmed to the ``Packages`` table alone: rpm's path/dependency lookup indexes
(Basenames, Providename, ...) are used only by rpm itself, dropping them roughly halves
the size.

The in-image database is already journal-parked at install time (install.py:parkdb); this
re-parks the copy so DROP/VACUUM leaves no -wal/-shm sidecar beside the declared output.
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

import finalize

# Matches package_system/rpm/install.py:DBPATH.
DBPATH = "usr/lib/sysimage/rpm/rpmdb.sqlite"


def _trim(db: Path) -> None:
    """Keep only the Packages table and compact the copy into a stable single file."""
    con = sqlite3.connect(db, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        # SQLite has no "DROP TABLE WHERE", so enumerate the tables rpm alone uses.
        doomed = [
            name
            for (name,) in con.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' "
                "AND name != 'Packages' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for name in doomed:
            con.execute(f'DROP TABLE "{name}"')
        con.execute("VACUUM")
    finally:
        con.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rpmdb")
    finalize.add_arguments(parser)
    parser.add_argument("--out", required=True, help="output rpmdb.sqlite")
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    with finalize.image(args, program="rpmdb") as tree:
        src = tree / DBPATH
        if not src.exists():
            raise SystemExit(f"no rpmdb at {src}; the image has no installed packages")
        shutil.copy2(src, out)
    _trim(out)
    print(f"rpmdb: captured Packages-only database -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
