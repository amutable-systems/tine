#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# The microcode the image installs reached the UKI's .ucode section, which the stub hands the kernel
# ahead of the initrds, as an uncompressed cpio naming one file per vendor, which is what the kernel's
# early loader looks for. Newc spells names in plain text, so a grep over the section finds them.
# Only an x86 kernel takes microcode that way, so anywhere else the section must be absent rather
# than empty.
set -euo pipefail

uki=$1 arch=$2

case $arch in
x86_64) vendors="AuthenticAMD GenuineIntel" ;;
arm64) vendors="" ;;
*) echo "uki-microcode: no microcode vendors known for $arch" >&2; exit 1 ;;
esac

if [ -z "$vendors" ]; then
    test "$(ukify inspect "$uki" | grep -cx ".ucode:")" = 0
    exit
fi

ucode=$(mktemp)
ukify inspect "$uki" --section ".ucode:binary@$ucode" > /dev/null
for vendor in $vendors; do
    grep -qa "kernel/x86/microcode/$vendor.bin" "$ucode"
done
