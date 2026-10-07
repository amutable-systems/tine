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
serves, which is why Buck cannot check them and go has to. The project's go.mod and the go.mod of
each module it replaces with a directory determine the build list, so no Go source file enters
this action. The resulting module cache doubles as the file:// proxy the offline build reads.
"""

import filecmp
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TypedDict

import specs
from util import fail


class Spec(TypedDict):
    # The project's go.mod, relative to the source directory. It is a key of `modules`.
    mod: str
    # The module cache directory to fill, `cache/download` in the proxy layout, kept across fetches.
    module_cache_dir: str
    # Maps each go.mod path, relative to the source directory, to the file to copy there. It
    # contains `mod` and the go.mod of each module that `mod` replaces with a directory.
    modules: dict[str, str]
    # The project's go.sum, which go checks a download against wherever it pins one.
    sum: str


def stage(spec: Spec, tree: Path) -> Path:
    """Copy the go.mod files and the go.sum into `tree`, and return the project's module directory."""
    assert spec["mod"] in spec["modules"], f"go-fetch: {spec['mod']} is not among the staged modules"

    # `go mod download` runs in the module directory and reads only go.mod and go.sum files. A
    # replace directive names a directory relative to the project's go.mod, so each go.mod is copied
    # to the same path in `tree` as in the source directory. The files are copies because go edits
    # go.sum in place when entries are missing.
    tree.mkdir()
    for path, source in spec["modules"].items():
        staged = tree / path
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, staged)
    module = (tree / spec["mod"]).parent
    shutil.copy(spec["sum"], module / "go.sum")
    return module


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "go-fetch", argv)
    # The sandbox points TMPDIR at /var/tmp, which it backs with the action's scratch space.
    scratch = Path(tempfile.gettempdir())
    module = stage(spec, scratch / "source")

    module_cache_dir = Path(spec["module_cache_dir"]).resolve()
    module_cache_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["go", "mod", "download"],
        check=True,
        cwd=module,
        env=os.environ
        | {
            "GOCACHE": str(scratch / "gocache"),
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
            # This action stages only go.mod and go.sum files, so a go.work in a parent directory
            # comes from outside the checkout. Without GOWORK=off, go would resolve modules with it.
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
