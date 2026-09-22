#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# Assert a built artifact mentions each of the things it is supposed to.
#
# Usage: contains.sh FILE PATTERN...   (each PATTERN is a basic regular expression)
set -euo pipefail

file=$1
shift
missing=0
for pattern in "$@"; do
    if ! grep -q -e "$pattern" "$file"; then
        echo "$(basename "$file"): missing $pattern" >&2
        missing=1
    fi
done
exit "$missing"
