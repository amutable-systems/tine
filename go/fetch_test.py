# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the files the Go module fetch stages.

buck test tine//go:test
"""

import tempfile
import unittest
from pathlib import Path
from typing import override

import fetch


class TestStage(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="go-fetch-test.", dir="/var/tmp"))
        )
        self.tree = self.scratch / "tree"

    def _spec(self, mod: str, *modules: str, pins: str) -> fetch.Spec:
        for path in [*modules, pins]:
            written = self.scratch / "src" / path
            written.parent.mkdir(parents=True, exist_ok=True)
            written.write_text(path, encoding="utf-8")
        return {
            "mod": mod,
            "module_cache_dir": str(self.scratch / "cache"),
            "modules": {path: str(self.scratch / "src" / path) for path in modules},
            "sum": str(self.scratch / "src" / pins),
        }

    def _staged(self) -> dict[str, str]:
        return {
            str(path.relative_to(self.tree)): path.read_text(encoding="utf-8")
            for path in self.tree.rglob("*")
            if path.is_file()
        }

    def test_replacements_keep_their_place_beside_the_module(self) -> None:
        spec = self._spec("server/go.mod", "server/go.mod", "api/go.mod", pins="server/go.sum")

        self.assertEqual(fetch.stage(spec, self.tree), self.tree / "server")
        self.assertEqual(
            self._staged(),
            {"api/go.mod": "api/go.mod", "server/go.mod": "server/go.mod", "server/go.sum": "server/go.sum"},
        )

    def test_a_module_at_the_root_of_the_source(self) -> None:
        spec = self._spec("go.mod", "go.mod", "internal/api/go.mod", pins="go.sum")

        self.assertEqual(fetch.stage(spec, self.tree), self.tree)
        self.assertEqual(
            self._staged(),
            {"go.mod": "go.mod", "go.sum": "go.sum", "internal/api/go.mod": "internal/api/go.mod"},
        )
