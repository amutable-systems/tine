"""Tests for the Go module finder.

    buck test tine//go:test

Sources reach the driver either as files, which a checkout in the consuming repository globs into,
or as one directory artifact holding a whole fetched tree. Both are exercised here.
"""

import tempfile
import unittest
from pathlib import Path
from typing import override

import workspace


class TestResolveWorkspace(unittest.TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="go-workspace-test.")
        self.addCleanup(tmp.cleanup)
        self.checkout = Path(tmp.name) / "checkout"
        self.checkout.mkdir()

    def _write(self, *paths: str) -> None:
        for path in paths:
            written = self.checkout / path
            written.parent.mkdir(parents=True, exist_ok=True)
            written.touch()

    def _files(self, *paths: str) -> dict[str, str]:
        """The sources a globbed checkout hands over: one entry per file."""
        self._write(*paths)
        return {f"hello/{path}": str(self.checkout / path) for path in paths}

    def test_finds_a_module_among_globbed_files(self) -> None:
        sources = self._files("go.mod", "go.sum", "main.go")

        self.assertEqual(
            workspace.resolve_workspace("hello", sources),
            {"mod": "hello/go.mod", "root": "hello", "sum": "hello/go.sum"},
        )

    def test_finds_a_module_inside_a_directory_artifact(self) -> None:
        self._write("go.mod", "go.sum", "cmd/hello/main.go")

        self.assertEqual(
            workspace.resolve_workspace("hello", {"fetched": str(self.checkout)}),
            {"mod": "fetched/go.mod", "root": "fetched", "sum": "fetched/go.sum"},
        )

    def test_a_module_resolving_nothing_pins_nothing(self) -> None:
        self._write("go.mod", "main.go")

        self.assertEqual(
            workspace.resolve_workspace("nodeps", {"fetched": str(self.checkout)}),
            {"mod": "fetched/go.mod", "root": "fetched", "sum": None},
        )

    def test_the_outermost_module_is_the_projects_own(self) -> None:
        """A nested go.mod is a helper module, which go leaves out of a `./...` build itself."""
        self._write("go.mod", "go.sum", "internal/tools/go.mod", "internal/tools/go.sum")

        self.assertEqual(
            workspace.resolve_workspace("hello", {"fetched": str(self.checkout)}),
            {"mod": "fetched/go.mod", "root": "fetched", "sum": "fetched/go.sum"},
        )

    def test_a_go_sum_below_the_root_is_not_the_projects_own(self) -> None:
        self._write("go.mod", "internal/tools/go.mod", "internal/tools/go.sum")

        self.assertIsNone(workspace.resolve_workspace("hello", {"fetched": str(self.checkout)})["sum"])

    def test_rejects_modules_that_are_not_one_project(self) -> None:
        other = self.checkout.parent / "other"
        other.mkdir()
        (other / "go.mod").touch()
        self._write("go.mod")

        with self.assertRaises(SystemExit) as caught:
            workspace.resolve_workspace(
                "hello",
                {"first": str(self.checkout), "second": str(other)},
            )
        self.assertEqual(
            str(caught.exception),
            "tine: go_package hello: ['second/go.mod'] is not nested in first/go.mod, so srcs hold no "
            "single project; narrow `srcs` to one module",
        )

    def test_rejects_a_workspace(self) -> None:
        self._write("go.mod", "go.work")

        with self.assertRaisesRegex(SystemExit, "go workspaces are not supported"):
            workspace.resolve_workspace("hello", {"fetched": str(self.checkout)})

    def test_rejects_sources_holding_no_module(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            workspace.resolve_workspace("hello", {"fetched": str(self.checkout)})
        self.assertEqual(
            str(caught.exception),
            "tine: go_package hello: srcs hold no go.mod; by default the checkout is expected in the "
            "hello/ directory, pass `srcs` when it lives elsewhere",
        )
