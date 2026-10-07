#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# Assert that a box with a parent from the same repositories is a layer over it, unless it opts out,
# and that one without a parent is not.
#
# Usage: layering.sh PARENT LAYERED STANDALONE   (each the space-separated layers of a box, lowest first)
set -euo pipefail

parent_only=usr/bin/bash
own=usr/bin/fish

read -r -a parent <<< "$1"
read -r -a layered <<< "$2"
read -r -a standalone <<< "$3"
failed=0
fail() {
    echo "$*" >&2
    failed=1
}

[ ${#parent[@]} -eq 1 ] || fail "the box without a parent has ${#parent[@]} layers instead of one"
[ -e "${parent[0]}/$parent_only" ] || fail "the box without a parent lacks $parent_only"
[ ! -e "${parent[0]}/$own" ] || fail "the box without a parent has its child's $own"

[ "${layered[*]}" = "${parent[*]} ${layered[-1]}" ] || fail "the layered box is not one layer over its parent's: ${layered[*]}"
[ -e "${layered[-1]}/$own" ] || fail "the layered box's own layer lacks $own"
[ ! -e "${layered[-1]}/$parent_only" ] || fail "the layered box's own layer installs its parent's $parent_only again"

[ ${#standalone[@]} -eq 1 ] || fail "the unlayered box has ${#standalone[@]} layers instead of one"
[ -e "${standalone[0]}/$own" ] || fail "the unlayered box lacks $own"
[ -e "${standalone[0]}/$parent_only" ] || fail "the unlayered box lacks its parent's $parent_only"
exit "$failed"
