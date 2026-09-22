#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# Assert a UAPI.16 manifest lists nothing outside the hierarchies the artifact it describes carries.
#
# Usage: manifest-scope.sh MANIFEST PREFIX...   (each PREFIX is a top-level name, such as usr)
set -euo pipefail

manifest=$1
shift
pattern=$(printf '\\|%s' "$@")
outside=$(grep -o '"name":"[^"]*"' "$manifest" | grep -v "^\"name\":\"\\(${pattern:2}\\)[/\"]" || true)
if [ -n "$outside" ]; then
    echo "$(basename "$manifest"): carries $* alone, so these do not belong:" >&2
    echo "$outside" >&2
    exit 1
fi
