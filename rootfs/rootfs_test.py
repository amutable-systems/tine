"""Tests for source overlays and root filesystem output capture."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import ExitStack, chdir
from pathlib import Path
from typing import override
from unittest import mock

import rootfs


class TestSourceOverlay(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="source-test.", dir="/var/tmp"))
        )
        self.enterContext(mock.patch.dict(os.environ, {"TMPDIR": str(self.scratch)}))
        self.enterContext(mock.patch.object(tempfile, "tempdir", None))
        self.source = self.scratch / "source"
        self.source.mkdir()
        (self.source / "original").write_text("original", encoding="utf-8")
        os.utime(self.source / "original", ns=(1234567890123456789, 1234567890123456789))
        (self.source / "readonly").mkdir(mode=0o555)
        (self.source / "link").symlink_to("original")
        (self.source / "dangling").symlink_to("missing")
        for name in (".wh.original", ".wh..wh..opq", ".esc.literal"):
            (self.source / name).write_text(name, encoding="utf-8")

    def test_preserves_sources_without_decoding_image_markers(self) -> None:
        with chdir(self.scratch), rootfs.source_overlay(Path("source"), Path("target")) as tree:
            self.assertEqual({p.name for p in tree.iterdir()}, {p.name for p in self.source.iterdir()})
            for source in self.source.iterdir():
                overlaid = tree / source.name
                self.assertEqual(overlaid.lstat().st_mode, source.lstat().st_mode)
                self.assertEqual(overlaid.lstat().st_mtime_ns, source.lstat().st_mtime_ns)
                if source.is_symlink():
                    self.assertEqual(overlaid.readlink(), source.readlink())
                elif source.is_file():
                    self.assertEqual(overlaid.read_bytes(), source.read_bytes())
            upper = next(self.scratch.glob("source.*/upper"))
            self.assertEqual(list(upper.iterdir()), [])
        self.assertEqual(list(self.scratch.glob("source.*")), [])

    def test_discards_writes_and_cleans_storage_after_success_and_failure(self) -> None:
        before = (self.source / "original").stat()
        target = self.scratch / "target"
        for fail in (False, True):
            with self.subTest(fail=fail):
                try:
                    with rootfs.source_overlay(self.source, target) as tree:
                        self.assertFalse((tree / "generated").exists())
                        (tree / "link").write_text("changed", encoding="utf-8")
                        (tree / "original").unlink()
                        (tree / "readonly").chmod(0o700)
                        (tree / "generated").mkdir(mode=0o500)
                        (tree / "back\\slash").touch()
                        (tree / "opaque").mkdir()
                        os.setxattr(tree / "opaque", "user.overlay.opaque", b"y")
                        if fail:
                            raise RuntimeError("build failed")
                except RuntimeError:
                    self.assertTrue(fail)
                self.assertEqual(list(target.iterdir()), [])
                self.assertEqual((self.source / "original").read_text(encoding="utf-8"), "original")
                self.assertEqual((self.source / "original").stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual((self.source / "readonly").stat().st_mode & 0o777, 0o555)
                self.assertFalse((self.source / "generated").exists())
                self.assertEqual(list(self.scratch.glob("source.*")), [])

    def test_open_files_do_not_leave_a_mounted_checkout(self) -> None:
        target = self.scratch / "target"
        with ExitStack() as stack:
            with rootfs.source_overlay(self.source, target) as tree:
                (tree / "generated").write_text("output", encoding="utf-8")
                opened = stack.enter_context((tree / "generated").open())
                descriptor = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
                stack.callback(os.close, descriptor)
            self.assertFalse(target.is_mount())
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(opened.read(), "output")
            self.assertEqual(list(self.scratch.glob("source.*")), [])
        self.assertEqual((self.source / "original").read_text(encoding="utf-8"), "original")

    def test_a_leaked_writer_does_not_fail_teardown(self) -> None:
        target = self.scratch / "target"
        writer = textwrap.dedent("""
            import itertools, os, sys
            for i in itertools.count():
                os.close(os.open(str(i), os.O_CREAT, dir_fd=int(sys.argv[1])))
        """)
        with rootfs.source_overlay(self.source, target) as tree:
            descriptor = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, descriptor)
            child = subprocess.Popen([sys.executable, "-c", writer, str(descriptor)], pass_fds=[descriptor])
            self.addCleanup(child.wait)
            self.addCleanup(child.kill)
            while not (tree / "0").exists():
                self.assertIsNone(child.poll())
        self.assertIsNone(child.poll())
        self.assertFalse(target.is_mount())
        self.assertFalse((self.source / "0").exists())

    def test_rootfs_paths_can_contain_overlay_option_separators(self) -> None:
        for name in ("we:ird", "com,ma", "back\\slash"):
            with self.subTest(name=name):
                lower = self.scratch / name
                lower.mkdir()
                (lower / "original").write_text("original", encoding="utf-8")
                with rootfs.rootfs(self.scratch / "target", lowers=[lower]) as tree:
                    self.assertEqual((tree / "original").read_text(encoding="utf-8"), "original")
                    (tree / "original").write_text("changed", encoding="utf-8")
                self.assertEqual((lower / "original").read_text(encoding="utf-8"), "original")


class TestSourceOverlayTree(unittest.TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="source-test.", dir="/var/tmp")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {"TMPDIR": str(self.root)}))
        self.enterContext(mock.patch.object(tempfile, "tempdir", None))
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

    def test_preserves_the_whole_tree(self) -> None:
        with (
            chdir(self.project),
            rootfs.source_overlay(Path("input"), self.root / "build/src") as tree,
        ):
            self.assertFalse(tree.is_symlink())
            original = {path.relative_to(self.source) for path in self.source.rglob("*")}
            self.assertEqual({path.relative_to(tree) for path in tree.rglob("*")}, original)
            for path in original:
                source, overlaid = self.source / path, tree / path
                self.assertEqual(overlaid.lstat().st_mode, source.lstat().st_mode)
                self.assertEqual(overlaid.lstat().st_mtime_ns, source.lstat().st_mtime_ns)
                if source.is_symlink():
                    self.assertEqual(overlaid.readlink(), source.readlink())
                elif source.is_file():
                    self.assertEqual(overlaid.read_bytes(), source.read_bytes())
            self.assertFalse((tree / "dangling-link").exists())
            self.assertFalse((tree / "absolute-link").exists())
            self.assertEqual((tree / "directory-link/file").read_text(encoding="utf-8"), "original")
            self.assertEqual((tree / "data/relative-link").read_text(encoding="utf-8"), "original")
            upper = next(self.root.glob("source.*/upper"))
            self.assertEqual(list(upper.iterdir()), [])

    def test_writes_and_deletions_leave_inputs_unchanged(self) -> None:
        # Mutations through upstream links must copy up their targets, including chmod.
        with (
            chdir(self.project),
            rootfs.source_overlay(Path("input"), self.root / "build/src") as tree,
        ):
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

    def test_mounts_unwind_on_success_and_failure(self) -> None:
        for fail in [False, True]:
            with self.subTest(fail=fail), chdir(self.project):
                scratch = self.root / str(fail)
                try:
                    with rootfs.source_overlay(Path("input"), scratch) as tree:
                        (tree / "new").touch()
                        if fail:
                            raise RuntimeError("build failed")
                except RuntimeError:
                    self.assertTrue(fail)
                self.assertEqual(list(scratch.iterdir()), [])


class TestCaptureOnExit(unittest.TestCase):
    def test_captures_after_success(self) -> None:
        output = Path("output")
        with mock.patch.object(rootfs, "capture") as capture:
            with rootfs.capture_on_exit(output):
                pass

        capture.assert_called_once_with(output)

    def test_capture_failure_does_not_hide_body_failure(self) -> None:
        output = Path("output")
        with (
            mock.patch.object(rootfs, "capture", side_effect=OSError("bad name")),
            self.assertRaisesRegex(RuntimeError, "transaction failed") as raised,
        ):
            with rootfs.capture_on_exit(output):
                raise RuntimeError("transaction failed")

        self.assertEqual(
            raised.exception.__notes__,
            ["rootfs capture of output also failed: OSError: bad name"],
        )

    def test_capture_failure_after_success_is_reported(self) -> None:
        with (
            mock.patch.object(rootfs, "capture", side_effect=OSError("bad name")),
            self.assertRaisesRegex(OSError, "bad name"),
            rootfs.capture_on_exit("output"),
        ):
            pass


if __name__ == "__main__":
    unittest.main()
