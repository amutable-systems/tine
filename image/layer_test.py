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


class TestCopyModes(unittest.TestCase):
    def test_mode_normalization(self) -> None:
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            source = tree / "src"
            (source / "sub").mkdir(parents=True)
            (source / "sub").chmod(0o775)
            (source / "sub/file").write_text("file")
            (source / "sub/file").chmod(0o664)
            (source / "sub/secret").write_text("secret")
            (source / "sub/secret").chmod(0o600)
            (source / "sub/script").write_text("script")
            (source / "sub/script").chmod(0o777)
            (source / "link").symlink_to("sub/file")
            # pre-existing tree, does not get touched
            destination = tree / "dst"
            destination.mkdir()
            (destination / "installed").write_text("installed")
            (destination / "installed").chmod(0o600)

            layer._copy(source, destination)

            self.assertEqual((destination / "sub").stat().st_mode & 0o777, 0o755)
            self.assertEqual((destination / "sub/file").stat().st_mode & 0o777, 0o644)
            self.assertEqual((destination / "sub/secret").stat().st_mode & 0o777, 0o644)
            self.assertEqual((destination / "sub/script").stat().st_mode & 0o777, 0o755)
            self.assertTrue((destination / "link").is_symlink())
            self.assertEqual((destination / "installed").stat().st_mode & 0o777, 0o600)


class TestWriteFile(unittest.TestCase):
    def test_default_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            written = Path(scratch) / "etc/tine/written"

            layer._apply_filesystem(["write_file", str(written), "content"])

            # buckd normalizes its umask to 022, validate that assumption
            self.assertEqual(written.stat().st_mode & 0o777, 0o644)
            self.assertEqual(written.parent.stat().st_mode & 0o777, 0o755)
