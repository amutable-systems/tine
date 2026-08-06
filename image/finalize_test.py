"""Tests for the image generators.

buck test tine//image:test
"""

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
