#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# Runs inside the image on Buck's own interpreter; the image itself has no python. The image is
# this script's root, so it writes image paths, and the project is the working directory, so the
# marker path it receives resolves the same as it would outside.

import shutil
import sys
from pathlib import Path

shutil.copy(sys.argv[1], "/etc/tine/from-python")
Path("/etc/tine/from-python-arg").write_text(f"{sys.argv[2]}\n")
