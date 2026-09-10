"""Tests for the Go build driver's checks.

    buck test tine//go:test

Exercise writable source views, package selection, and build commands. The box carries no Go toolchain.
"""

import errno
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from typing import override
from unittest.mock import patch

import build


class TestBuildTree(unittest.TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="go-build-test.", dir="/var/tmp")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = self.root / "project"
        self.source = self.project / "input"
        (self.source / "data").mkdir(parents=True)
        (self.source / "data/file").write_text("original", encoding="utf-8")
        (self.source / "data/file").chmod(0o444)
        (self.source / ".hidden").touch()
        (self.source / "file-link").symlink_to("data/file")
        (self.source / "directory-link").symlink_to("data")
        (self.source / "dangling-link").symlink_to("missing")
        (self.source / "data/relative-link").symlink_to("../file-link")
        (self.source / "cycle").symlink_to("cycle")
        (self.source / "absolute-link").symlink_to(self.root / "absent")
        (self.source / "empty").mkdir()
        (self.project / "outside").write_text("outside", encoding="utf-8")
        (self.source / "absolute-input").symlink_to(self.project / "outside")
        (self.source / "relative-input").symlink_to("../../project/outside")

    def test_preserves_the_whole_tree_without_copying_on_read(self) -> None:
        with chdir(self.project), build._build_tree(Path("input"), self.root / "build") as tree:
            self.assertFalse(tree.is_symlink())
            original = {path.relative_to(self.source) for path in self.source.rglob("*")}
            self.assertEqual({path.relative_to(tree) for path in tree.rglob("*")}, original)
            for path in original:
                source, overlaid = self.source / path, tree / path
                self.assertEqual(overlaid.lstat().st_mode, source.lstat().st_mode)
                if source.is_symlink():
                    self.assertEqual(overlaid.readlink(), source.readlink())
                elif source.is_file():
                    self.assertEqual(overlaid.read_bytes(), source.read_bytes())
            self.assertFalse((tree / "dangling-link").exists())
            self.assertFalse((tree / "absolute-link").exists())
            self.assertEqual((tree / "directory-link/file").read_text(encoding="utf-8"), "original")
            self.assertEqual((tree / "data/relative-link").read_text(encoding="utf-8"), "original")
            upper = next((self.root / "build").glob("overlay.*/upper"))
            self.assertEqual(list(upper.iterdir()), [])

    def test_writes_and_deletions_leave_inputs_unchanged(self) -> None:
        # Mutations through upstream links must copy up their targets, including chmod.
        with chdir(self.project), build._build_tree(Path("input"), self.root / "build") as tree:
            (tree / "data/file").chmod(0o600)
            (tree / "data/relative-link").write_text("changed", encoding="utf-8")
            self.assertEqual((tree / "file-link").read_text(encoding="utf-8"), "changed")
            (tree / ".hidden").unlink()
            (tree / "dangling-link").unlink()
            (tree / "new").touch()
            self.assertFalse((tree / ".hidden").exists())
            self.assertFalse((tree / "dangling-link").is_symlink())

        self.assertEqual((self.source / "data/file").read_text(encoding="utf-8"), "original")
        self.assertEqual((self.source / "data/file").stat().st_mode & 0o777, 0o444)
        self.assertTrue((self.source / ".hidden").is_file())
        self.assertEqual((self.source / "dangling-link").readlink(), Path("missing"))
        self.assertFalse((self.source / "new").exists())

    def test_links_back_into_the_project_are_readonly(self) -> None:
        with chdir(self.project), build._build_tree(Path("input"), self.root / "build") as tree:
            for name in ["absolute-input", "relative-input"]:
                with self.subTest(name=name):
                    self.assertEqual((tree / name).read_text(encoding="utf-8"), "outside")
                    with self.assertRaises(OSError) as caught:
                        (tree / name).write_text("changed", encoding="utf-8")
                    self.assertEqual(caught.exception.errno, errno.EROFS)
            self.assertEqual((self.project / "outside").read_text(encoding="utf-8"), "outside")

    def test_mounts_unwind_on_success_and_failure(self) -> None:
        for fail in [False, True]:
            with self.subTest(fail=fail), chdir(self.project):
                scratch = self.root / str(fail)
                try:
                    with build._build_tree(Path("input"), scratch) as tree:
                        (tree / "new").touch()
                        if fail:
                            raise RuntimeError("build failed")
                except RuntimeError:
                    self.assertTrue(fail)
                self.assertEqual(list(scratch.iterdir()), [scratch / "src"])
                self.assertEqual(list((scratch / "src").iterdir()), [])
                (self.project / "outside").write_text("writable again", encoding="utf-8")


class TestBuildCommand(unittest.TestCase):
    def test_output_name_and_linker_flags(self) -> None:
        self.assertEqual(
            build._build_command(Path("/var/tmp/binaries/etcd"), "example.com/server/v3", ["-s", "-w"]),
            ["go", "build", "-o", "/var/tmp/binaries/etcd", "-ldflags=-s -w", "example.com/server/v3"],
        )

    def test_builds_the_selected_package(self) -> None:
        self.assertEqual(
            build._build_command(Path("/var/tmp/binaries/tool"), "example.com/mycmd/v2", []),
            ["go", "build", "-o", "/var/tmp/binaries/tool", "example.com/mycmd/v2"],
        )


class TestPackage(unittest.TestCase):
    def test_resolves_root_relative_and_import_paths(self) -> None:
        for selector in [".", "./cmd/server", "example.com/server/v3"]:
            with self.subTest(selector=selector), patch("build.subprocess.run") as run:
                run.return_value.stdout = '{"ImportPath": "example.com/server/v3", "Name": "main"}'
                self.assertEqual(
                    build._package(selector, Path("workspace"), {"GOFLAGS": "-tags=test"}),
                    "example.com/server/v3",
                )
                run.assert_called_once_with(
                    ["go", "list", "-json=ImportPath,Name", selector],
                    check=True,
                    cwd=Path("workspace"),
                    env={"GOFLAGS": "-tags=test"},
                    stdout=subprocess.PIPE,
                    text=True,
                )

    def test_rejects_libraries_empty_and_multiple_matches(self) -> None:
        main = '{"ImportPath": "example.com/server", "Name": "main"}'
        for listing in ["", '{"ImportPath": "example.com/lib", "Name": "lib"}', main + main]:
            with self.subTest(listing=listing), patch("build.subprocess.run") as run:
                run.return_value.stdout = listing
                with self.assertRaisesRegex(SystemExit, "must resolve to exactly one main package"):
                    build._package("./...", Path("workspace"), {})

    def test_rejects_flags_files_and_empty_selectors(self) -> None:
        for selector in ["", "-help", "main.go"]:
            with self.subTest(selector=selector), patch("build.subprocess.run") as run:
                with self.assertRaisesRegex(SystemExit, "invalid package selection"):
                    build._package(selector, Path("workspace"), {})
                run.assert_not_called()

    def test_propagates_missing_or_excluded_package_errors(self) -> None:
        with patch("build.subprocess.run", side_effect=subprocess.CalledProcessError(1, "go list")):
            with self.assertRaises(subprocess.CalledProcessError):
                build._package("./missing", Path("workspace"), {})
