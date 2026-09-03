#!/bin/bash
# A box is an input to every action that runs inside it, so its bytes are part of those actions'
# cache keys. Wall-clock state anywhere in a box therefore costs every package, Go and Rust build
# its cache hit, on a schedule nobody is watching. Assert that neither root box carries any.
#
# Only what a directory artifact's digest covers is in scope: file contents, names and the
# executable bit. Modes and mtimes drift freely and Buck normalizes the former when it
# materializes the tree, so they cannot move a cache key and are not checked here.
set -euo pipefail

rpmbox=$1 alpmbox=$2
r=0

fail() {
    echo "box-state: $*" >&2
    r=1
}

absent() {
    local box=$1 path
    shift
    for path in "$@"; do
        if [ -e "$box/$path" ]; then
            fail "$box carries $path"
        fi
    done
}

for box in "$rpmbox" "$alpmbox"; do
    id=$(cat "$box/etc/machine-id")
    if [ "$id" != uninitialized ]; then
        fail "$box has machine-id '$id' rather than the marker systemd initializes on first boot"
    fi
    # systemd's seed, and ldconfig's cache of inode numbers and mtimes.
    absent "$box" var/lib/systemd/random-seed var/cache/ldconfig/aux-cache
done

# rpm stamps INSTALLTIME from SOURCE_DATE_EPOCH and advances one second per package, so the range
# is fixed by the closure rather than by when the build ran. rpm resolves --dbpath itself, which
# is why that one is absolute.
db="$PWD/$rpmbox/usr/lib/sysimage/rpm"
count=$(rpm -qa --dbpath "$db" | wc -l)
if [ "$count" -eq 0 ]; then
    fail "$rpmbox has no installed packages"
else
    stamps=$(rpm -qa --dbpath "$db" --qf '%{INSTALLTIME}\n' | sort -n)
    first=$(head -1 <<< "$stamps") last=$(tail -1 <<< "$stamps")
    latest=$((SOURCE_DATE_EPOCH + count - 1))
    if [ "$first" -lt "$SOURCE_DATE_EPOCH" ] || [ "$last" -gt "$latest" ]; then
        fail "$rpmbox install times run $first..$last, outside $SOURCE_DATE_EPOCH..$latest"
    fi
fi
# The transaction's own bookkeeping, and the side files sqlite recreates on demand.
absent "$rpmbox" usr/lib/sysimage/libdnf5 usr/lib/sysimage/rpm/rpmdb.sqlite-wal \
    usr/lib/sysimage/rpm/rpmdb.sqlite-shm usr/lib/sysimage/rpm/.rpm.lock

# alpm records one install date per package, which the installer rewrites to the epoch outright.
dates=$(grep -A1 -h '%INSTALLDATE%' "$alpmbox"/var/lib/pacman/local/*/desc | grep -E '^[0-9]+$' | sort -u)
if [ -z "$dates" ]; then
    fail "$alpmbox has no installed packages"
elif [ "$dates" != "$SOURCE_DATE_EPOCH" ]; then
    fail "$alpmbox install dates are $(paste -sd' ' <<< "$dates"), not just $SOURCE_DATE_EPOCH"
fi
absent "$alpmbox" var/log/pacman.log var/lib/pacman/sync

exit "$r"
