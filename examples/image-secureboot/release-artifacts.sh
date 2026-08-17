#!/bin/bash
# The release directory is where every published name meets. Assert the set the example publishes,
# that each listing is named after what it describes, that the verity pair is named after the two
# halves of the root hash (which is what lets systemd-sysupdate give the partitions it writes the
# UUIDs dissection pairs them by), and that the ESP is not among them.
set -euo pipefail

directory=$1 roothash_file=$2
roothash=$(cat "$roothash_file")
names=$(ls "$directory")

missing=0
for expected in image_0_x86-64.raw image_0_x86-64.qcow2 image_0_x86-64.efi \
    image_0_x86-64.vmlinuz image_0_x86-64.initrd demo-ext_0_x86-64.sysext.raw \
    image_0_x86-64.Uapi16Manifest image_0_x86-64.esp.Uapi16Manifest \
    image_0_x86-64.usr.Uapi16Manifest demo-ext_0_x86-64.sysext.Uapi16Manifest \
    "image_0_x86-64.usr-x86-64.${roothash:0:32}.raw" \
    "image_0_x86-64.usr-x86-64-verity.${roothash:32:32}.raw"; do
    grep -qx "$expected" <<< "$names" || { echo "release: $expected is missing" >&2; missing=1; }
done
# Its listing is published, because what it carries is on the disk; the partition itself is not.
if grep '\.raw$' <<< "$names" | grep -q esp; then
    echo "release: the ESP partition must not be published" >&2
    missing=1
fi
exit "$missing"
