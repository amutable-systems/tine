#!/bin/bash
# A package database is captured as one file, named after the format the package system keeps it in.
# rpm keeps one sqlite database, so the file is that. alpm keeps a directory of per-package entries,
# so the file is a tar of them.
set -euo pipefail

rpmdb=$1 alpmdb=$2
r=0

if [ "$(basename "$rpmdb")" != pkgdb.sqlite ]; then
    echo "pkgdb: rpm captured $(basename "$rpmdb")" >&2
    r=1
elif [ "$(head -c 15 "$rpmdb")" != "SQLite format 3" ]; then
    echo "pkgdb: $rpmdb is not an sqlite database" >&2
    r=1
fi

if [ "$(basename "$alpmdb")" != pkgdb.tar.zst ]; then
    echo "pkgdb: alpm captured $(basename "$alpmdb")" >&2
    r=1
else
    entries=$(zstdcat "$alpmdb" | tar -t)
    if ! grep -q '/desc$' <<< "$entries"; then
        echo "pkgdb: $alpmdb carries no package entries" >&2
        r=1
    fi
    # Dropped on capture: pacman alone reads it, and it is the largest part of an entry.
    if grep -q '/mtree$' <<< "$entries"; then
        echo "pkgdb: $alpmdb still carries the per-package mtree manifests" >&2
        r=1
    fi
fi
exit "$r"
