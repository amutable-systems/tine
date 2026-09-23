#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# Each distribution's archive carries the shared tree and only its own file, or none where the
# select chose no source.
#
# Usage: tree-test.sh ARCHIVE DISTRIBUTION|none ...
set -euo pipefail

status=0
while [ $# -gt 0 ]; do
    archive=$1
    expected=$2
    shift 2
    listing=$(tar -tf "$archive" | sed 's|^\./||')
    if ! grep -qx 'etc/tine/tree-shared' <<<"$listing"; then
        echo "$(basename "$archive"): missing the shared tree" >&2
        status=1
    fi
    if [ "$expected" = none ]; then
        if grep -qx 'etc/tine/distribution' <<<"$listing"; then
            echo "$(basename "$archive"): carries another distribution's tree" >&2
            status=1
        fi
        continue
    fi
    actual=$(tar -xOf "$archive" --wildcards '*etc/tine/distribution')
    if [ "$actual" != "$expected" ]; then
        echo "$(basename "$archive"): tree says '$actual', expected '$expected'" >&2
        status=1
    fi
done
exit "$status"
