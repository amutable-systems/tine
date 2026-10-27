# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Fail when a rebuild after a box change changes the Secure Boot example.

    buck run tine//tests:rebuild-reproducibility

Every action that runs in a box takes box/sandbox.py as an input. Appending a comment to box/sandbox.py
reruns every such action, as a change to the box does. The test removes the comment on exit. It builds in
its own isolation dir to leave the developer's build state alone.
"""

import json
import os
import re
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import cast

from util import buck_output, fail

IMAGE = "tine//examples/image-secureboot:image.fedora"
TARGETS = [
    IMAGE,
    f"{IMAGE}[partitions][usr]",
    f"{IMAGE}[partitions][usr-verity]",
    f"{IMAGE}[partitions][usr-verity-sig]",
    f"{IMAGE}[uki]",
    f"{IMAGE}[boot][uki]",
    f"{IMAGE}[roothash]",
]
# If the second build reruns neither the key generation nor the disk assembly, it compares the outputs of
# the first build with themselves.
RERUN = {
    ("tine//examples/image-secureboot:signing", "signing_key"),
    ("tine//examples/image-secureboot:pcr-signing", "signing_key"),
    ("tine//examples/image-secureboot:image", "ddi disk"),
}
# `what-ran` names an action `<target> (<configuration>) (<category> <identifier>)`.
ACTION = re.compile(r"(?P<target>\S+) \(\S+\) \((?P<category>[^)]+)\)")
ISOLATION = "rebuild-reproducibility"
SANDBOX = Path("box/sandbox.py")


class Rebuild:
    """Run nested `tine buck` commands in the test's isolation dir."""

    def __init__(self, root: Path) -> None:
        self.root = root
        # The outer `tine buck run` exports BUCK2_BINARY, BUCK2_ARG0 and TINE_MOUNTS. Without them, the
        # nested bin/tine resolves Buck and sets up the mounts itself, as it does for a developer.
        self.environment = {
            name: value
            for name, value in os.environ.items()
            if name not in ("BUCK2_BINARY", "BUCK2_ARG0", "TINE_MOUNTS")
        }

    def tine(self, *arguments: str) -> str:
        """Run `tine buck` in the isolation dir and return its stdout. Buck's progress goes to stderr."""
        command = [str(self.root / "bin" / "tine"), "buck", "--isolation-dir", ISOLATION, *arguments]
        print("+", " ".join(command[1:]), file=sys.stderr, flush=True)
        proc = subprocess.run(
            command, cwd=self.root, env=self.environment, stdout=subprocess.PIPE, text=True
        )
        if proc.returncode != 0:
            print(proc.stdout, end="", file=sys.stderr, flush=True)
            fail(f"tine buck {' '.join(arguments)} exited with {proc.returncode}")
        return proc.stdout

    def build(self) -> dict[str, Path]:
        """Build TARGETS and return the content-based path of each output."""
        outputs: dict[str, Path] = {}
        for line in self.tine("build", "--show-full-output", *TARGETS).splitlines():
            target, _, path = line.partition(" ")
            if target in TARGETS:
                # Buck prints a symlink whose path contains the configuration hash, and that path stays
                # the same across rebuilds. The symlink points to the content-based path, which changes
                # when the content changes.
                outputs[target] = Path(path).resolve()
        if missing := [target for target in TARGETS if target not in outputs]:
            fail(f"the build printed no output for {', '.join(missing)}")
        return outputs

    def ran(self) -> set[tuple[str, str]]:
        """Return the target and category of each action that the last build ran."""
        found: set[tuple[str, str]] = set()
        for line in self.tine("log", "what-ran", "--format", "json").splitlines():
            if not line.startswith("{"):
                continue  # bin/tine prints its own notes between the JSON lines.
            record = json.loads(line)
            if action := ACTION.fullmatch(cast(str, record["identity"])):
                found.add((action["target"], action["category"]))
        return found


def main() -> None:
    root = Path(buck_output("root", "--kind", "project"))
    sandbox = root / SANDBOX
    original = sandbox.read_bytes()

    with ExitStack() as stack:
        run = Rebuild(root)
        # `clean` deletes the isolation dir's buck-out, which contains the image and its boxes.
        stack.callback(run.tine, "clean")

        print("=== first build", file=sys.stderr)
        first = run.build()

        print(f"=== second build, with a comment appended to {SANDBOX}", file=sys.stderr)
        stack.callback(sandbox.write_bytes, original)
        sandbox.write_bytes(
            original + b"# tine//tests:rebuild-reproducibility reruns every action in a box\n"
        )
        second = run.build()

        if missing := sorted(RERUN - run.ran()):
            fail(f"the second build did not rerun {', '.join(f'{t} ({c})' for t, c in missing)}")
        if differ := [
            f"{target}: {first[target]} and {second[target]}"
            for target in TARGETS
            if first[target] != second[target]
        ]:
            fail("the rebuild changed outputs:\n" + "\n".join(differ))
        print("=== the rebuild reproduced every output", file=sys.stderr)


if __name__ == "__main__":
    main()
