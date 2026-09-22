#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# What the disk boots, extracted out of its own ESP. The selection is semantic (the newest kernel,
# its UKI, the sections inside it), so it can go wrong without any target failing: assert by magic
# that each artifact is what it claims to be, and that the extracted initrd is the one the UKI
# carries rather than the initrd image the composition was handed.
set -euo pipefail

kernel=$1 uki=$2 initrd=$3 composed=$4

# A bzImage carries "HdrS" at 0x202 and a PE binary "MZ" at 0.
test "$(dd if="$kernel" bs=1 skip=514 count=4 status=none)" = HdrS
test "$(dd if="$uki" bs=1 count=2 status=none)" = MZ

# The UKI appends the modules initrd to the one it was given, so the extracted one is larger.
test "$(stat -c%s "$initrd")" -gt "$(stat -c%s "$composed")"
