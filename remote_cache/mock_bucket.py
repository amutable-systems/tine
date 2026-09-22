# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Mock bucket for read testing.

Reads in this design are plain `GET`s of one key, so a directory behind `http.server` is a faithful
stand-in for a public bucket. It counts requests and do failure injection, which seaweedfs cannot do.

Tests write into that directory directly; the S3 writer is exercised against `seaweed.Seaweed`.
"""

import functools
import http.server
import os
import threading
from collections import Counter
from pathlib import Path
from typing import override

from store import write_atomically


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the directory, counts every key asked for, and can be told to fail one of them."""

    counts: Counter[str]
    agents: set[str]
    broken: set[str]

    @override
    def do_GET(self) -> None:  # noqa: N802 - the base class names it
        key = self.path.lstrip("/")
        self.counts[key] += 1
        self.agents.add(self.headers.get("User-Agent", ""))
        if key in self.broken:
            self.send_error(503, "pretending to be unwell")
            return
        super().do_GET()

    # `format` because that is what the base class calls it, and an override has to match.
    @override
    def log_message(self, format: str, *args: object) -> None:
        """Quiet: a test asserts on counts, not on a request log."""


class Served:
    """A directory reachable over HTTP for as long as this is open.

    Implements the `bucket.Writer` protocol as well, with direct file writes.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.counts: Counter[str] = Counter()
        self.agents: set[str] = set()
        self.broken: set[str] = set()
        bound = {"counts": self.counts, "agents": self.agents, "broken": self.broken}
        handler = functools.partial(type("Bound", (Handler,), bound), directory=str(root))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        # A short poll, because `shutdown` waits out one interval and a suite closes many of these.
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def keys(self, prefix: str) -> list[str]:
        """Every object stored under a prefix, by name."""
        directory = self.root / prefix
        return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []

    def put(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomically(path, data)

    def refresh(self, key: str) -> bool:
        try:
            os.utime(self.root / key)
        except FileNotFoundError:
            return False
        return True

    def describe(self) -> str:
        return str(self.root)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self) -> Served:
        return self

    def __exit__(self, *details: object) -> None:
        self.close()
