# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

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

    def _write(self, *paths: str, text: str = "") -> None:
        for path in paths:
            written = self.checkout / path
            written.parent.mkdir(parents=True, exist_ok=True)
            written.write_text(text, encoding="utf-8")

    def test_finds_a_module_inside_a_directory_artifact(self) -> None:
        self._write("go.mod", "go.sum", "cmd/hello/main.go")

        self.assertEqual(
            workspace.resolve_workspace("hello", self.checkout),
            {"mod": "go.mod", "modules": ["go.mod"], "root": "", "sum": "go.sum"},
        )

    def test_finds_a_nested_module(self) -> None:
        self._write("hello/go.mod", "hello/go.sum")

        self.assertEqual(
            workspace.resolve_workspace("hello", self.checkout),
            {"mod": "hello/go.mod", "modules": ["hello/go.mod"], "root": "hello", "sum": "hello/go.sum"},
        )

    def test_a_module_resolving_nothing_pins_nothing(self) -> None:
        self._write("go.mod", "main.go")

        self.assertEqual(
            workspace.resolve_workspace("nodeps", self.checkout),
            {"mod": "go.mod", "modules": ["go.mod"], "root": "", "sum": None},
        )

    def test_the_outermost_module_is_the_projects_own(self) -> None:
        """A nested go.mod is a helper module, which go leaves out of a `./...` build itself."""
        self._write("go.mod", "go.sum", "internal/tools/go.mod", "internal/tools/go.sum")

        self.assertEqual(
            workspace.resolve_workspace("hello", self.checkout),
            {"mod": "go.mod", "modules": ["go.mod"], "root": "", "sum": "go.sum"},
        )

    def test_a_go_sum_below_the_root_is_not_the_projects_own(self) -> None:
        self._write("go.mod", "internal/tools/go.mod", "internal/tools/go.sum")

        self.assertIsNone(workspace.resolve_workspace("hello", self.checkout)["sum"])

    def test_stages_the_go_mod_of_each_local_replacement_in_src(self) -> None:
        self._write("go.sum", "internal/api/go.mod", "internal/tools/go.mod")
        self._write(
            "go.mod",
            text="replace example.com/api => ./internal/api\nreplace example.com/missing => ./missing\n",
        )

        self.assertEqual(
            workspace.resolve_workspace("hello", self.checkout)["modules"],
            ["go.mod", "internal/api/go.mod"],
        )

    def test_rejects_replacements_outside_src(self) -> None:
        for directory in ["../outside", "./internal/../../outside", "/srv/outside"]:
            self._write("go.mod", text=f"replace example.com/outside => {directory}\n")
            with (
                self.subTest(directory=directory),
                self.assertRaisesRegex(
                    SystemExit, f"go.mod replaces a module with {directory}, which is outside src"
                ),
            ):
                workspace.resolve_workspace("hello", self.checkout)

    def test_a_module_resolving_nothing_stages_no_replacement(self) -> None:
        self._write("api/go.mod")
        self._write("go.mod", text="replace example.com/api => ./api\n")

        self.assertEqual(workspace.resolve_workspace("hello", self.checkout)["modules"], ["go.mod"])

    def test_rejects_modules_that_are_not_one_project(self) -> None:
        self._write("first/go.mod", "second/go.mod")

        with self.assertRaises(SystemExit) as caught:
            workspace.resolve_workspace("hello", self.checkout)
        self.assertEqual(
            str(caught.exception),
            "tine: go_package hello: ['second/go.mod'] is not nested in first/go.mod, so src holds no "
            "single project; set `module_root` to select a module",
        )

    def test_rejects_sources_holding_no_module(self) -> None:
        for module_root in [None, "server"]:
            with self.subTest(module_root=module_root), self.assertRaises(SystemExit) as caught:
                workspace.resolve_workspace("hello", self.checkout, module_root)
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
            {"mod": "go.mod", "modules": ["go.mod"], "root": "", "sum": "go.sum"},
        )

    def test_selects_the_server_module_in_an_etcd_checkout(self) -> None:
        self._write("go.mod", "go.sum", "api/go.mod", "client/v3/go.mod", "server/go.sum")
        self._write("server/go.mod", text="replace go.etcd.io/etcd/api/v3 => ../api\n")

        self.assertEqual(
            workspace.resolve_workspace("etcd", self.checkout, "server"),
            {
                "mod": "server/go.mod",
                "modules": ["api/go.mod", "server/go.mod"],
                "root": "server",
                "sum": "server/go.sum",
            },
        )

    def test_selects_a_module_among_siblings_without_a_root_module(self) -> None:
        self._write("api/go.mod", "server/go.mod")

        self.assertEqual(
            workspace.resolve_workspace("etcd", self.checkout, "server"),
            {"mod": "server/go.mod", "modules": ["server/go.mod"], "root": "server", "sum": None},
        )

    def test_selects_a_module_nested_in_another(self) -> None:
        self._write(
            "go.mod",
            "go.sum",
            "server/go.mod",
            "server/go.sum",
            "server/tools/go.mod",
            "server/tools/go.sum",
        )

        self.assertEqual(
            workspace.resolve_workspace("etcd", self.checkout, "server/tools"),
            {
                "mod": "server/tools/go.mod",
                "modules": ["server/tools/go.mod"],
                "root": "server/tools",
                "sum": "server/tools/go.sum",
            },
        )

    def test_the_selected_module_uses_only_its_own_pins(self) -> None:
        self._write("go.mod", "go.sum", "server/go.mod")

        self.assertIsNone(workspace.resolve_workspace("etcd", self.checkout, "server")["sum"])

    def test_does_not_search_for_a_module_below_it(self) -> None:
        self._write("go.mod", "server/nested/go.mod")

        for module_root in ["server", "missing", "server/nested/go.mod"]:
            with self.subTest(module_root=module_root), self.assertRaises(SystemExit) as caught:
                workspace.resolve_workspace("etcd", self.checkout, module_root)
            self.assertEqual(
                str(caught.exception),
                f"tine: go_package etcd: module_root {module_root!r} is not a directory in src that "
                "contains a go.mod; src contains ['go.mod', 'server/nested/go.mod']",
            )

    def test_rejects_an_empty_module_root(self) -> None:
        self._write("go.mod")

        with self.assertRaisesRegex(SystemExit, "module_root must not be empty"):
            workspace.resolve_workspace("etcd", self.checkout, "")

    def test_rejects_absolute_and_parent_paths_to_an_existing_module(self) -> None:
        self._write("go.mod", "server/go.mod")

        for module_root in [str(self.checkout / "server"), "../checkout/server", "server/../server"]:
            with (
                self.subTest(module_root=module_root),
                self.assertRaisesRegex(SystemExit, "is not a directory in src that contains a go.mod"),
            ):
                workspace.resolve_workspace("etcd", self.checkout, module_root)

    def test_rejects_selection_through_directory_symlinks(self) -> None:
        self._write("server/go.mod")
        outside = self.checkout.parent / "outside"
        (outside / "server").mkdir(parents=True)
        (outside / "server/go.mod").touch()
        (self.checkout / "outside").symlink_to(outside)
        (self.checkout / "alias").symlink_to("server")

        for module_root in ["outside/server", "alias"]:
            with (
                self.subTest(module_root=module_root),
                self.assertRaisesRegex(SystemExit, "is not a directory in src that contains a go.mod"),
            ):
                workspace.resolve_workspace("etcd", self.checkout, module_root)


class TestLocalReplacements(unittest.TestCase):
    def test_reads_directories_from_both_forms(self) -> None:
        text = (
            "module example.com/server\n"
            "\n"
            "require example.com/api v0.0.0\n"
            "\n"
            "replace example.com/api => ../api\n"
            "replace example.com/slashes => ./a//b\n"
            "replace(\n"
            "\t// example.com/commented => ./commented\n"
            '\texample.com/quoted => "./with space"\n'
            "\texample.com/raw v1.2.3 => `./raw`\n"
            "\texample.com/parent => .. // the parent module\n"
            "\texample.com/absolute => /srv/absolute\n"
            ")\n"
        )

        self.assertEqual(
            workspace.local_replacements(text),
            ["../api", "./a//b", "./with space", "./raw", "..", "/srv/absolute"],
        )

    def test_skips_modules_and_other_directives(self) -> None:
        text = (
            "module example.com/server\n"
            "require (\n"
            "\texample.com/api v0.0.0 // => ./api\n"
            ")\n"
            "replace example.com/fork => example.com/fork v1.0.0\n"
            "replace example.com/versioned => ./versioned v1.0.0\n"
            'replace example.com/unterminated => "./api\n'
            "replace example.com/self => .\n"
            "exclude example.com/old v0.1.0\n"
        )

        self.assertEqual(workspace.local_replacements(text), [])
