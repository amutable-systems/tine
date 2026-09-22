#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Download the modules a Go project's go.sum pins, ahead of its offline build.

This is the one online step of a Go build, and go itself is the verifier: a download is checked
against the committed go.sum where that has an entry for it, and against the checksum database
otherwise, because `go mod download` deliberately does not extend go.sum (golang.org/issue/45332).
Which means the committed go.sum is enforced by the offline build rather than here: it refuses to
use a module the go.sum does not pin, so nothing unpinned can reach a binary either way.

The go.sum's h1: hashes are dirhashes over a module's contents, not hashes of the bytes a proxy
serves, which is why Buck cannot check them and go has to. The go.mod alone determines the build
list, so no source ever enters this action, and the resulting module cache doubles as the file://
proxy the offline build reads.
"""

import filecmp
import os
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

import specs
from util import fail


class Spec(TypedDict):
    # The project's go.mod.
    mod: str
    # The module cache directory to fill, `cache/download` in the proxy layout, kept across fetches.
    module_cache_dir: str
    # The project's go.sum, which go checks a download against wherever it pins one.
    sum: str


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "go-fetch", argv)

    # `go mod download` wants a module directory, and these two files are all it reads. They are
    # staged as copies because go edits go.sum in place when entries are missing.
    module = Path("/var/tmp/module")
    module.mkdir()
    shutil.copy(spec["mod"], module / "go.mod")
    shutil.copy(spec["sum"], module / "go.sum")

    module_cache_dir = Path(spec["module_cache_dir"]).resolve()
    module_cache_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["go", "mod", "download"],
        check=True,
        cwd=module,
        env=os.environ
        | {
            "GOCACHE": "/var/tmp/gocache",
            # Only the user's env file, never $GOROOT/go.env, which is why everything this action
            # depends on is spelled out below instead of left to the box's go.
            "GOENV": "off",
            # The cache below is a declared output, Buck has to be able to delete and write it
            "GOFLAGS": "-modcacherw",
            "GOMODCACHE": str(module_cache_dir),
            # Where the modules come from and what vouches for the ones go.sum does not pin. Both
            # are go's upstream defaults, but a distribution is free to patch them: Fedora shipped
            # `direct` with no checksum database for a while, which would have made this action
            # trust whatever a repository served.
            "GOPROXY": "https://proxy.golang.org,direct",
            "GOSUMDB": "sum.golang.org",
            # The box's go is the toolchain; never fetch another one.
            "GOTOOLCHAIN": "local",
            # This action stages a bare go.mod, so a go.work anywhere above it could only be a
            # stray from outside the checkout. In workspace mode go would resolve against that.
            "GOWORK": "off",
        },
    )

    # Downloading does not extend go.sum, but repairing a go.mod that disagrees with it writes the
    # module graph's go.mod hashes into the staged go.sum on the way. So a go.sum that comes back
    # changed is one with incomplete pins, which the build would fail on further along.
    if not filecmp.cmp(spec["sum"], module / "go.sum", shallow=False):
        fail(
            "go-fetch: go.sum is missing go.mod hashes of the module graph; "
            "run `go mod tidy` and commit the result"
        )


if __name__ == "__main__":
    main()
