#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# A package database is captured as one file, named after the format the package system keeps it in.
# rpm keeps one sqlite database, so the file is that. alpm keeps a directory of per-package entries,
# so the file is a tar of them. The caller states the expected path and format.
set -euo pipefail

pkgdb=$1 format=$2
r=0

if [ "$(basename "$pkgdb")" != "pkgdb.$format" ]; then
    echo "pkgdb: captured $(basename "$pkgdb"), expected pkgdb.$format" >&2
    exit 1
fi

case $format in
sqlite)
    if [ "$(head -c 15 "$pkgdb")" != "SQLite format 3" ]; then
        echo "pkgdb: $pkgdb is not an sqlite database" >&2
        r=1
    fi
    ;;
tar.zst)
    entries=$(zstdcat "$pkgdb" | tar -t)
    if ! grep -q '/desc$' <<< "$entries"; then
        echo "pkgdb: $pkgdb carries no package entries" >&2
        r=1
    fi
    # Dropped on capture: pacman alone reads it, and it is the largest part of an entry.
    if grep -q '/mtree$' <<< "$entries"; then
        echo "pkgdb: $pkgdb still carries the per-package mtree manifests" >&2
        r=1
    fi
    ;;
*)
    echo "pkgdb: no check for the format $format" >&2
    r=1
    ;;
esac
exit "$r"
