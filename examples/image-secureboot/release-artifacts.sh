#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# The release directory is where every published name meets. Assert the set the example publishes,
# that each listing is named after what it describes, that the verity pair is named after the two
# halves of the root hash (which is what lets systemd-sysupdate give the partitions it writes the
# UUIDs dissection pairs them by), and that the ESP is not among them.
set -euo pipefail

directory=$1 roothash_file=$2 arch=$3
roothash=$(cat "$roothash_file")
names=$(ls "$directory")

image="image_0_$arch" extension="demo-ext_0_$arch"
missing=0
for expected in "$image.raw" "$image.qcow2" "$image.efi" "$image.vmlinuz" "$image.initrd" \
    "$extension.sysext.raw" "$image.Uapi16Manifest" "$image.esp.Uapi16Manifest" \
    "$image.usr.Uapi16Manifest" "$extension.sysext.Uapi16Manifest" \
    "$image.spdx.json" "$image.cdx.json" "$image.pkgdb.sqlite" \
    "$image.initrd.spdx.json" "$image.initrd.cdx.json" \
    "$extension.spdx.json" "$extension.cdx.json" \
    "$image.usr-$arch.${roothash:0:32}.raw" \
    "$image.usr-$arch-verity.${roothash:32:32}.raw"; do
    grep -qx "$expected" <<< "$names" || { echo "release: $expected is missing" >&2; missing=1; }
done
# Its listing is published, because what it carries is on the disk; the partition itself is not.
if grep '\.raw$' <<< "$names" | grep -q esp; then
    echo "release: the ESP partition must not be published" >&2
    missing=1
fi
exit "$missing"
