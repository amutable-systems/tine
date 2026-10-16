# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Run a go_package's own Go toolchain instead of the box's go."""

import os
from pathlib import Path


def env(goroot: Path, base: dict[str, str]) -> dict[str, str]:
    """Return the environment variables that make a `go` command run the toolchain in `goroot`."""
    # The drivers run go with cwd= set to a module directory. A relative GOROOT would then point into
    # that directory, and go would fail to find its standard library.
    goroot = goroot.absolute()
    return {
        "GOROOT": str(goroot),
        "PATH": os.pathsep.join([str(goroot / "bin"), base.get("PATH", os.defpath)]),
    }
