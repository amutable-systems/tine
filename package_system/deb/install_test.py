# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the content rules a dpkg transaction is filtered by.

buck test tine//package_system/deb:test
"""

import tempfile
import unittest
from pathlib import Path

import debfile

import install


class TestPaths(unittest.TestCase):
    def test_keeps_everything_when_nothing_is_filtered(self) -> None:
        self.assertEqual(install._paths(docs=True, langs=[]), [])
        keeps = install._keeps([])
        self.assertTrue(keeps("/usr/share/doc/bash/README"))
        self.assertTrue(keeps("/usr/share/locale/de/LC_MESSAGES/bash.mo"))

    def test_a_later_rule_carves_the_licenses_back_out(self) -> None:
        keeps = install._keeps(install._paths(docs=False, langs=[]))
        self.assertFalse(keeps("/usr/share/doc/bash/README"))
        self.assertFalse(keeps("/usr/share/man/man1/bash.1"))
        self.assertTrue(keeps("/usr/share/doc/bash/copyright"))
        self.assertTrue(keeps("/usr/bin/bash"))

    def test_a_later_rule_carves_the_kept_languages_back_out(self) -> None:
        keeps = install._keeps(install._paths(docs=True, langs=["en", "de"]))
        self.assertTrue(keeps("/usr/share/locale/en/LC_MESSAGES/bash.mo"))
        self.assertTrue(keeps("/usr/share/locale/de/LC_MESSAGES/bash.mo"))
        self.assertFalse(keeps("/usr/share/locale/fr/LC_MESSAGES/bash.mo"))

    def test_both_renderings_state_the_same_rules_in_the_same_order(self) -> None:
        rules = install._paths(docs=False, langs=["en"])
        arguments = install._dpkg_paths(rules)

        # dpkg applies these in order and a later one wins, which is the rule `_keeps` replays for
        # the pass dpkg does not perform. The two must not be able to disagree.
        self.assertEqual(
            arguments,
            [f"--path-{'include' if include else 'exclude'}={path}" for include, path in rules],
        )
        self.assertEqual(arguments[-1], "--path-include=/usr/share/locale/en/*")

    def test_a_glob_does_not_cross_a_directory_boundary_differently_than_dpkg(self) -> None:
        # dpkg matches with fnmatch(3) and no FNM_PATHNAME, so `*` spans `/`; Python's
        # fnmatch.fnmatchcase does the same, which is what lets one rule serve both passes.
        keeps = install._keeps(install._paths(docs=False, langs=[]))
        self.assertFalse(keeps("/usr/share/doc/bash/html/deep/page.html"))
        self.assertFalse(keeps("/usr/share/man/fr/man1/bash.1"))


class TestEmptyClosure(unittest.TestCase):
    def _closure(self, root: Path) -> Path:
        empty = root / "closure"
        empty.mkdir()
        return empty

    def test_refuses_a_root_with_nothing_installed_underneath(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            installroot = root / "root"
            installroot.mkdir()
            with self.assertRaises(SystemExit):
                install.install(self._closure(root), installroot, arch="x86_64", langs=[], docs=True)

    def test_writes_nothing_when_a_lower_layer_already_satisfies_it(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            installroot = root / "root"
            admin = installroot / debfile.ADMINDIR
            admin.mkdir(parents=True)
            (admin / "status").write_text("Package: bash\n")
            stamped = (admin / "status").stat().st_mtime_ns

            install.install(self._closure(root), installroot, arch="x86_64", langs=[], docs=True)

            # A `touch` is a setattr, and overlayfs copies a lower file up on one, so an empty
            # layer that prepared the database would carry the whole thing in its delta.
            self.assertEqual((admin / "status").stat().st_mtime_ns, stamped)
            self.assertFalse((admin / "updates").exists())


if __name__ == "__main__":
    unittest.main()
