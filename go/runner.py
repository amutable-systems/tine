#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Run the test binaries that a go_package's build compiled, inside a box."""

import json
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from typing import TypedDict, cast

import specs

import rootfs

# go test passes these flags to every test binary it runs. A flag in `args` comes later on the command
# line, and the test binary takes the last value of a flag.
_DEFAULT_ARGS = ["-test.paniconexit0", "-test.timeout=10m0s"]


class Spec(TypedDict):
    # Arguments for each test binary, such as -test.short.
    args: list[str]
    # The project's source directory artifact.
    src: str
    # The go_package's test binaries and their manifest.
    tests: str


def run_tests(spec: Spec) -> int:
    """Run each test binary in its package directory, and return 1 if any of them fails."""
    # A test binary runs in its package directory, so the path of `tests` has to be absolute.
    tests = Path(spec["tests"]).absolute()
    # build.py writes the manifest: one entry per test binary, with the binary's file name, the package's
    # import path and the package's directory relative to src.
    manifest = cast(list[dict[str, str]], json.loads((tests / "manifest.json").read_text(encoding="utf-8")))
    failed = False
    with ExitStack() as stack:
        # The sources are a build output, or the checkout itself in dev mode. A test that writes to
        # its package directory would change either one.
        stack.enter_context(rootfs.readonly_project(Path.cwd(), {}))
        for test in manifest:
            start = time.monotonic()
            status = subprocess.run(
                [tests / test["binary"], *_DEFAULT_ARGS, *spec["args"]],
                check=False,
                cwd=Path(spec["src"]) / test["dir"],
            ).returncode
            elapsed = time.monotonic() - start
            print(f"{'ok  ' if status == 0 else 'FAIL'}\t{test['package']}\t{elapsed:.3f}s", flush=True)
            failed = failed or status != 0
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run_tests(specs.parse(Spec, "go-test", argv)))


if __name__ == "__main__":
    main()
