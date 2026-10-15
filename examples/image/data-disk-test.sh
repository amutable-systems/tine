#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# Check that a disk without an ESP partition in its definitions contains its one partition and no ESP.
#
# Usage: data-disk-test.sh DISK
set -euo pipefail

table=$(sfdisk --json "$1")
python3 - "$table" <<'PYTHON'
import json
import sys

ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
partitions = json.loads(sys.argv[1])["partitiontable"]["partitions"]
types = [partition["type"].upper() for partition in partitions]
if len(types) != 1 or ESP in types:
    sys.exit(f"expected one partition and no ESP, found the partition types {types}")
PYTHON
