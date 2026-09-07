"""Tests for the Go module finder.

    buck test tine//go:test

The source directory can contain a module at its root or nested inside a fetched tree.
"""

import tempfile
import unittest
from pathlib import Path
from typing import override

import workspace


class TestResolveWorkspace(unittest.TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="go-workspace-test.", dir="/var/tmp")
        self.addCleanup(tmp.cleanup)
        self.checkout = Path(tmp.name) / "checkout"
        self.checkout.mkdir()

    def _write(self, *paths: str) -> None:
        for path in paths:
            written = self.checkout / path
            written.parent.mkdir(parents=True, exist_ok=True)
            written.touch()

    def test_finds_a_module_inside_a_directory_artifact(self) -> None:
        self._write("go.mod", "go.sum", "cmd/hello/main.go")

        self.assertEqual(
            workspace.resolve_workspace("hello", self.checkout),
            {"mod": "go.mod", "root": "", "sum": "go.sum"},
        )

    def test_finds_a_nested_module(self) -> None:
        self._write("hello/go.mod", "hello/go.sum", "hello/main.go")

        self.assertEqual(
            workspace.resolve_workspace("hello", self.checkout),
            {"mod": "hello/go.mod", "root": "hello", "sum": "hello/go.sum"},
        )

    def test_a_module_resolving_nothing_pins_nothing(self) -> None:
        self._write("go.mod", "main.go")

        self.assertEqual(
            workspace.resolve_workspace("nodeps", self.checkout),
            {"mod": "go.mod", "root": "", "sum": None},
        )

    def test_the_outermost_module_is_the_projects_own(self) -> None:
        """A nested go.mod is a helper module, which go leaves out of a `./...` build itself."""
        self._write("go.mod", "go.sum", "internal/tools/go.mod", "internal/tools/go.sum")

        self.assertEqual(
            workspace.resolve_workspace("hello", self.checkout),
            {"mod": "go.mod", "root": "", "sum": "go.sum"},
        )

    def test_a_go_sum_below_the_root_is_not_the_projects_own(self) -> None:
        self._write("go.mod", "internal/tools/go.mod", "internal/tools/go.sum")

        self.assertIsNone(workspace.resolve_workspace("hello", self.checkout)["sum"])

    def test_rejects_modules_that_are_not_one_project(self) -> None:
        self._write("first/go.mod", "second/go.mod")

        with self.assertRaises(SystemExit) as caught:
            workspace.resolve_workspace("hello", self.checkout)
        self.assertEqual(
            str(caught.exception),
            "tine: go_package hello: ['second/go.mod'] is not nested in first/go.mod, so src holds no "
            "single project; narrow `src` to one module",
        )

    def test_rejects_a_workspace(self) -> None:
        self._write("go.mod", "go.work")

        with self.assertRaisesRegex(SystemExit, "go workspaces are not supported"):
            workspace.resolve_workspace("hello", self.checkout)

    def test_a_live_checkout_may_hold_a_go_work(self) -> None:
        self._write("go.mod", "go.work")

        self.assertEqual(
            workspace.resolve_workspace("hello", self.checkout, live=True),
            {"mod": "go.mod", "root": "", "sum": None},
        )

    def test_workspace_discovery_skips_build_output_and_vcs_state(self) -> None:
        self._write("go.mod")
        for name in ("target", ".git", ".jj", ".hg", ".svn"):
            self._write(f"{name}/go.mod", f"{name}/go.sum", f"{name}/go.work")

        self.assertEqual(
            workspace.resolve_workspace("nodeps", self.checkout),
            {"mod": "go.mod", "root": "", "sum": None},
        )

    def test_rejects_sources_holding_no_module(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            workspace.resolve_workspace("hello", self.checkout)
        self.assertEqual(
            str(caught.exception),
            "tine: go_package hello: src holds no go.mod; by default the checkout is expected in the "
            "hello/ directory, pass `src` when it lives elsewhere",
        )

    def test_rejects_individual_files_and_missing_inputs(self) -> None:
        self._write("go.mod")
        for source in [self.checkout / "go.mod", self.checkout / "missing"]:
            with self.subTest(source=source), self.assertRaisesRegex(SystemExit, "src must be a directory"):
                workspace.resolve_workspace("hello", source)

    def test_does_not_follow_directory_symlinks_or_dangling_fixtures(self) -> None:
        self._write("go.mod", "go.sum")
        (self.checkout / "cycle").symlink_to(".")
        (self.checkout / "dangling").symlink_to("missing")

        self.assertEqual(
            workspace.resolve_workspace("hello", self.checkout),
            {"mod": "go.mod", "root": "", "sum": "go.sum"},
        )
