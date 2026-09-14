"""Prove the shared cache end to end: a real Buck publishes through the shim, and a cold one gets it back.

    buck run tine//tests:cache-roundtrip

Everything the shim does on its own is unit-tested in remote_cache/. What only a real build shows is
that `tine buck` starts and shares the shim, that Buck's client and the shim agree on the protocol,
and that a result one build published is a hit for a machine holding nothing but the bucket. So this
stands up a SeaweedFS bucket, points a `tine.local.toml` at it, builds the Go example cold as a
builder, then cleans, stops the shim, drops its store, and builds again as a reader, whose shim tells
Buck it takes no uploads: Buck has to believe that rather than try and be refused.

The Go example because its module fetch and its build are separate cached actions, so a hit carries
a directory of modules as well as a binary. A dedicated isolation dir, so the developer's own build
state is neither cleaned nor served by a cache-configured daemon.
"""

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import cast

import cache_shim
from util import buck_output, fail, nested_buck

import seaweed

TARGETS = ("tine//examples/image-go-project:hello", "tine//examples/image-go-project:nodeps")
ISOLATION = "cache-roundtrip"
BUCKET = "tine-cache"
# `tine.LOCAL_SETTINGS`, which this cannot import without dragging the whole launcher in.
LOCAL_SETTINGS = "tine.local.toml"
# Counters a healthy round trip never touches.
QUIET = (
    "bucket errors",
    "bundles gone",
    "bundles refused",
    "bundles too large",
    "incomplete",
    "pointers refused",
    "publish failures",
)


def toml(table: Mapping[str, object]) -> str:
    """The `[cache]` table as TOML."""
    lines = ["[cache]"]
    for key, value in table.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, str):
            lines.append(f"{key} = {value!r}".replace("'", '"'))
        else:
            assert isinstance(value, list)
            lines.append(f"{key} = [{', '.join(repr(one).replace(chr(39), chr(34)) for one in value)}]")
    return "\n".join(lines) + "\n"


class RoundTrip:
    """One round trip's moving parts: the checkout, the bucket, the settings and the shim."""

    def __init__(self, root: Path, scratch: Path, weed: seaweed.Seaweed) -> None:
        self.root = root
        self.reader: dict[str, object] = {
            "read_url": weed.read_url,
            "authority": [str(scratch / "ca.pem")],
            "dir": str(scratch / "store"),
        }
        self.builder = self.reader | {
            "s3_bucket": BUCKET,
            "s3_endpoint": weed.s3_endpoint,
            "s3_key_file": str(scratch / "s3.key"),
            "s3_insecure": True,
            "signing_key": str(scratch / "leaf.key"),
            "signing_certificate": str(scratch / "leaf.pem"),
        }
        (scratch / "s3.key").write_text("unchecked unchecked\n")
        self.settle(self.builder)
        # A nested command has to behave as a developer's would: start the shim rather than assume
        # the caller did, and run the launcher rather than the Buck the caller exported.
        self.environment = {
            name: value
            for name, value in os.environ.items()
            if name not in ("BUCK2_BINARY", "BUCK2_ARG0", "TINE_MOUNTS")
        }

    def settle(self, table: dict[str, object]) -> None:
        """Write the `[cache]` table the next builds run under."""
        (self.root / LOCAL_SETTINGS).write_text(toml(table))
        cache = cache_shim.settings({"cache": table}, self.root, LOCAL_SETTINGS)
        assert cache is not None
        self.cache = cache

    def tine(self, *arguments: str) -> str:
        """`tine buck` call in the round trip's isolation dir; echo and return its output."""
        command = [str(self.root / "bin" / "tine"), "buck", "--isolation-dir", ISOLATION, *arguments]
        print("+", " ".join(command[1:]), file=sys.stderr, flush=True)
        proc = subprocess.run(
            command,
            cwd=self.root,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        print(proc.stdout, end="", file=sys.stderr, flush=True)
        if proc.returncode != 0:
            fail(f"tine buck {' '.join(arguments)} exited with {proc.returncode}")
        return proc.stdout

    def build(self) -> int:
        """Build the targets, returning how many commands Buck took from the cache."""
        output = self.tine("build", *TARGETS)
        found = re.search(r"Commands: \d+ \(cached: (\d+),", output)
        if found is None:
            fail("Buck reported no command summary")
        return int(found[1])

    def report(self) -> dict[str, int]:
        """The shim's counters."""
        report = cache_shim.ask(self.cache.dir)
        if report is None:
            fail(f"nothing serves {self.cache.dir} after a build that needed it")
        counts = cast(dict[str, int], report["counts"])
        print(f"shim counters: {counts}", file=sys.stderr, flush=True)
        if noisy := [f"{what} {counts[what]}" for what in QUIET if counts.get(what)]:
            fail(f"the shim reports trouble: {', '.join(noisy)}; see {cache_shim.log_path(self.cache)}")
        return counts

    def stop_shim(self) -> None:
        """Stop the shim and wait until it is gone, so the next build starts one with an empty store."""
        report = cache_shim.ask(self.cache.dir)
        if report is None:
            return
        os.kill(cast(int, report["pid"]), signal.SIGTERM)
        deadline = time.monotonic() + 30
        while cache_shim.ask(self.cache.dir) is not None:
            if time.monotonic() > deadline:
                fail(f"the shim, pid {report['pid']}, did not stop")
            time.sleep(0.2)

    def teardown(self) -> None:
        self.stop_shim()
        # `clean` stops the isolation dir's daemon as well as emptying it.
        self.tine("clean")
        (self.root / LOCAL_SETTINGS).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--weed", type=Path, required=True, help="the SeaweedFS binary")
    arguments = parser.parse_args()

    buck = nested_buck()
    root = Path(buck_output(buck, "root", "--kind", "project"))
    if (root / LOCAL_SETTINGS).exists():
        fail(f"{root / LOCAL_SETTINGS} exists, and this writes one of its own; move it away first")

    with ExitStack() as stack:
        scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="tine-cache-roundtrip.")))
        weed = seaweed.Seaweed(arguments.weed, scratch, BUCKET)
        stack.callback(weed.close)
        # Create CA/leaf keys. No settings yet, so no shim is started.
        subprocess.run(
            [buck, "-v", "0", "run", "tine//remote_cache:test-ca", "--", str(scratch)], check=True
        )
        run = RoundTrip(root, scratch, weed)
        stack.callback(run.teardown)

        print("=== cold build: nothing in the bucket", file=sys.stderr)
        if (cached := run.build()) != 0:
            fail(f"a cold build took {cached} commands from a cache that was empty")
        cold = run.report()
        if not cold.get("published"):
            fail(f"the cold build published nothing: {cold}")

        print("=== warm build: a cleaned checkout, a reader shim, an empty store", file=sys.stderr)
        run.tine("clean")
        run.stop_shim()
        shutil.rmtree(run.cache.dir)
        run.settle(run.reader)
        cached = run.build()
        warm = run.report()
        # A reader advertises that it takes no uploads, and Buck honours that: not even its write
        # probe arrives. A refusal here means a Buck that no longer reads the capability, and a
        # developer who would now see an upload warning per build.
        if warm.get("uploads refused"):
            fail(f"Buck offered uploads to a reader that advertised taking none: {warm}")

        # Buck and the shim have to agree on every hit, and each hit has to be something the cold
        # build published. Not an exact count against what was published: it includes Buck's
        # own write probe.
        hits = warm.get("hits", 0)
        if not hits or hits != cached:
            fail(f"Buck took {cached} commands from the cache where the shim counted {warm}")
        if hits > cold["published"]:
            fail(f"{hits} hits out of a bucket that was published {cold['published']} results")
        print(f"=== {hits} results published cold and served warm out of the bucket", file=sys.stderr)


if __name__ == "__main__":
    main()
