# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the image generators.

buck test tine//image:test
"""

import os
import tempfile
import unittest
from pathlib import Path
from typing import override

import finalize


class TestKernelVersions(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tree = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="finalize.")))
        self.modules = self.tree / "usr/lib/modules"

    def test_an_image_without_modules_gives_depmod_nothing_to_do(self) -> None:
        self.assertEqual(finalize.kernel_versions(self.tree), [])

    def test_finds_every_installed_kernel(self) -> None:
        for version in ("6.19.0-1", "7.2.0-0.rc5"):
            (self.modules / version).mkdir(parents=True)
            (self.modules / version / "modules.order").touch()
        self.assertEqual(finalize.kernel_versions(self.tree), ["6.19.0-1", "7.2.0-0.rc5"])

    def test_skips_a_directory_left_behind_for_a_kernel_that_is_not_installed(self) -> None:
        (self.modules / "6.19.0-1/updates").mkdir(parents=True)
        self.assertEqual(finalize.kernel_versions(self.tree), [])


class TestHwdb(unittest.TestCase):
    def test_writes_the_database_the_image_ships_in_usr(self) -> None:
        self.assertEqual(
            finalize.hwdb_command("/usr/bin/systemd-hwdb", Path("/buildroot"), usr=True, strict=True),
            ["/usr/bin/systemd-hwdb", "--root=/buildroot", "--usr", "--strict", "update"],
        )

    def test_leaves_out_the_flags_it_was_told_not_to_pass(self) -> None:
        self.assertEqual(
            finalize.hwdb_command("/usr/bin/systemd-hwdb", Path("/buildroot"), usr=False, strict=False),
            ["/usr/bin/systemd-hwdb", "--root=/buildroot", "update"],
        )


class TestLocales(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tree = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="finalize.")))
        (self.tree / "etc").mkdir()

    def test_an_image_without_locale_gen_generates_nothing(self) -> None:
        self.assertFalse(finalize.wants_locales(self.tree))

    def test_a_commented_out_locale_gen_asks_for_nothing(self) -> None:
        (self.tree / "etc/locale.gen").write_text("# en_US.UTF-8 UTF-8\n\n")
        self.assertFalse(finalize.wants_locales(self.tree))

    def test_an_uncommented_locale_is_a_request(self) -> None:
        (self.tree / "etc/locale.gen").write_text("# a comment\nen_US.UTF-8 UTF-8\n")
        self.assertTrue(finalize.wants_locales(self.tree))


class TestNormalizeModes(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tree = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="finalize.", dir="/var/tmp")))

    def test_modes(self) -> None:
        tree = self.tree
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

        finalize.normalize_modes(tree)

        self.assertEqual(tree.stat().st_mode & 0o7777, 0o755)
        self.assertEqual((tree / "sub").stat().st_mode & 0o7777, 0o755)
        self.assertEqual((tree / "sub/file").stat().st_mode & 0o7777, 0o644)
        self.assertEqual((tree / "sub/secret").stat().st_mode & 0o7777, 0o644)
        self.assertEqual((tree / "sub/script").stat().st_mode & 0o7777, 0o755)
        self.assertEqual((tree / "sub/mount").stat().st_mode & 0o7777, 0o755)
        self.assertTrue((tree / "link").is_symlink())
        # not content: whatever it is, it is not ours to rewrite
        self.assertEqual((tree / "fifo").lstat().st_mode & 0o7777, 0o600)

    def test_the_directories_a_materialized_layer_comes_back_with(self) -> None:
        # Buck keeps no directory mode, so a layer out of the cache has the materializing host's umask.
        for name in ("usr", "usr/share", "usr/share/empty.sshd"):
            (self.tree / name).mkdir(mode=0o775)
        finalize.normalize_modes(self.tree)
        for name in ("usr", "usr/share", "usr/share/empty.sshd"):
            self.assertEqual((self.tree / name).stat().st_mode & 0o7777, 0o755)

    def test_leaves_a_skipped_subtree_alone(self) -> None:
        (self.tree / "run/tine/repart").mkdir(parents=True, mode=0o775)
        (self.tree / "run/tine/repart/0.raw").write_text("blocks")
        (self.tree / "run/tine/repart/0.raw").chmod(0o664)
        (self.tree / "usr").mkdir(mode=0o775)
        finalize.normalize_modes(self.tree, skip=[self.tree / "run/tine/repart/0.raw"])
        self.assertEqual((self.tree / "run/tine/repart/0.raw").stat().st_mode & 0o7777, 0o664)
        self.assertEqual((self.tree / "run/tine/repart").stat().st_mode & 0o7777, 0o755)
        self.assertEqual((self.tree / "usr").stat().st_mode & 0o7777, 0o755)

    def test_the_temporary_directories_get_their_mode_back_afterwards(self) -> None:
        # The order image() applies them in: normalization first, the sticky bit restored after.
        (self.tree / "tmp").mkdir(mode=0o1777)
        finalize.normalize_modes(self.tree)
        self.assertEqual((self.tree / "tmp").stat().st_mode & 0o7777, 0o755)
        finalize.world_writable(self.tree)
        self.assertEqual((self.tree / "tmp").stat().st_mode & 0o7777, 0o1777)


class TestWorldWritable(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tree = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="finalize.")))

    def test_restores_the_mode_a_layer_could_not_carry(self) -> None:
        for name in ("tmp", "var/tmp"):
            (self.tree / name).mkdir(parents=True, mode=0o755)
        finalize.world_writable(self.tree)
        for name in ("tmp", "var/tmp"):
            self.assertEqual((self.tree / name).stat().st_mode & 0o7777, 0o1777)

    def test_leaves_an_image_that_ships_neither_alone(self) -> None:
        finalize.world_writable(self.tree)
        self.assertFalse((self.tree / "tmp").exists())

    def test_does_not_chmod_through_a_symlink(self) -> None:
        (self.tree / "var").mkdir()
        (self.tree / "elsewhere").mkdir(mode=0o755)
        (self.tree / "var/tmp").symlink_to("../elsewhere")
        finalize.world_writable(self.tree)
        self.assertEqual((self.tree / "elsewhere").stat().st_mode & 0o7777, 0o755)
