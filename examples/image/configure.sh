#!/usr/bin/env bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# Runs inside the image with its own binaries. The project is mounted at the working
# directory, so the marker path this receives resolves the same as it would outside.
set -eu

cp "$1" /etc/tine/from-script
echo "$2" >/etc/tine/from-script-arg
