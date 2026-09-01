"""Tests for image layer filesystem operations.

buck test tine//image:test
"""

import os
import tempfile
import unittest
from pathlib import Path

import layer


class TestRemove(unittest.TestCase):
    def test_plain_path_is_also_a_glob(self) -> None:
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            removed = tree / "removed"
            removed.write_text("removed")

            layer._remove_glob(tree, "/removed")

            self.assertFalse(removed.exists())

    def test_recursive_glob_includes_hidden_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            (tree / "root.txt").write_text("root")
            (tree / ".hidden.txt").write_text("hidden")
            (tree / "sub").mkdir()
            (tree / "sub/nested.txt").write_text("nested")
            (tree / "sub/kept").write_text("kept")

            layer._remove_glob(tree, "/**/*.txt")

            self.assertEqual(
                {path.relative_to(tree) for path in tree.rglob("*")}, {Path("sub"), Path("sub/kept")}
            )

    def test_glob_removes_matching_directory_trees(self) -> None:
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            (tree / "var/cache/one/nested").mkdir(parents=True)
            (tree / "var/cache/two").write_text("two")
            (tree / "var/cache-kept").mkdir()

            layer._remove_glob(tree, "/var/cache/*")

            self.assertEqual(list((tree / "var/cache").iterdir()), [])
            self.assertTrue((tree / "var/cache-kept").is_dir())

    def test_unmatched_glob_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            kept = tree / "kept"
            kept.write_text("kept")

            layer._remove_glob(tree, "/missing-*")

            self.assertTrue(kept.is_file())

    def test_pattern_must_be_an_absolute_image_path(self) -> None:
        for pattern in ("relative/*", "/safe/../outside"):
            with self.subTest(pattern=pattern), self.assertRaises(SystemExit):
                layer._remove_glob(Path("/"), pattern)


class TestClampMtimes(unittest.TestCase):
    def test_clamps_only_newer_timestamps(self) -> None:
        epoch = 1_000_000_000
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            packaged = tree / "packaged"
            packaged.write_text("packaged")
            os.utime(packaged, (12345, 12345))
            (tree / "sub").mkdir()
            fresh = tree / "sub/fresh"
            fresh.write_text("fresh")
            # Dangling on purpose: following it would fail, clamping it must not.
            link = tree / "sub/link"
            link.symlink_to("missing")

            layer._clamp_mtimes(tree, epoch)

            self.assertEqual(packaged.lstat().st_mtime, 12345)
            self.assertEqual(fresh.lstat().st_mtime, epoch)
            self.assertEqual((tree / "sub").lstat().st_mtime, epoch)
            self.assertEqual(link.lstat().st_mtime, epoch)
            # The tree itself is an inode a terminal format records too.
            self.assertEqual(tree.lstat().st_mtime, epoch)

    def test_clamps_atime_mtime_separately(self) -> None:
        """A file read long before it was written keeps the older access time."""
        epoch = 1_000_000_000
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            written = Path(scratch) / "written-after-the-epoch"
            written.write_text("written")
            os.utime(written, (12345, epoch + 1))

            layer._clamp_mtimes(Path(scratch), epoch)

            self.assertEqual(written.lstat().st_atime, 12345)
            self.assertEqual(written.lstat().st_mtime, epoch)


class TestNormalization(unittest.TestCase):
    def test_modes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            tree.chmod(0o700)
            (tree / "sub").mkdir()
            (tree / "sub").chmod(0o1777)
            (tree / "sub/file").write_text("file")
            (tree / "sub/file").chmod(0o664)
            (tree / "sub/secret").write_text("secret")
            (tree / "sub/secret").chmod(0o600)
            (tree / "sub/script").write_text("script")
            (tree / "sub/script").chmod(0o777)
            (tree / "sub/mount").write_text("mount")
            (tree / "sub/mount").chmod(0o4755)
            (tree / "link").symlink_to("sub/secret")
            os.mkfifo(tree / "fifo", 0o600)

            layer._normalize_modes(tree)

            self.assertEqual(tree.stat().st_mode & 0o7777, 0o755)
            self.assertEqual((tree / "sub").stat().st_mode & 0o7777, 0o755)
            self.assertEqual((tree / "sub/file").stat().st_mode & 0o7777, 0o644)
            self.assertEqual((tree / "sub/secret").stat().st_mode & 0o7777, 0o644)
            self.assertEqual((tree / "sub/script").stat().st_mode & 0o7777, 0o755)
            self.assertEqual((tree / "sub/mount").stat().st_mode & 0o7777, 0o755)
            self.assertTrue((tree / "link").is_symlink())
            # not content: whatever it is, it is not ours to rewrite
            self.assertEqual((tree / "fifo").lstat().st_mode & 0o7777, 0o600)
