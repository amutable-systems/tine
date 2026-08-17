# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for Debian Deb822 parsing.

buck test tine//package_system/deb:test
"""

import gzip
import io
import tempfile
import unittest
from pathlib import Path

import deb822


class TestStanzas(unittest.TestCase):
    def test_reads_stanzas_case_insensitively_and_unfolds_values(self) -> None:
        source = io.StringIO(
            "Origin: Debian\nArchitectures: amd64\n\nPackage: demo\nDescription:\n a demo\n of folding\n\n"
        )
        self.assertEqual(
            list(deb822.stanzas(source)),
            [
                {"origin": "Debian", "architectures": "amd64"},
                {"package": "demo", "description": "\na demo\nof folding"},
            ],
        )

    def test_reads_the_field_names_policy_allows(self) -> None:
        source = io.StringIO("X_Vendor.Id: 7\nPython-Version: 3\n")
        self.assertEqual(list(deb822.stanzas(source)), [{"x_vendor.id": "7", "python-version": "3"}])

    def test_rejects_malformed_input(self) -> None:
        for content in (
            " continuation\n",
            "Package demo\n",
            "Package: one\npackage: two\n",
            "#Package: one\n",
            "-Package: one\n",
            "Pack age: one\n",
            "Päckage: one\n",
        ):
            with self.subTest(content=content), self.assertRaises(SystemExit):
                list(deb822.stanzas(io.StringIO(content)))


class TestRequired(unittest.TestCase):
    def test_rejects_a_missing_or_empty_field(self) -> None:
        self.assertEqual(deb822.required({"size": "42"}, "size", "record"), "42")
        for stanza in ({}, {"size": ""}):
            with self.subTest(stanza=stanza), self.assertRaises(SystemExit):
                deb822.required(stanza, "size", "record")


class TestOpenText(unittest.TestCase):
    def test_detects_gzip_by_content(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "Packages.not-gz"
            path.write_bytes(gzip.compress(b"Package: demo\n\n"))
            with deb822.open_text(path) as source:
                self.assertEqual(list(deb822.stanzas(source)), [{"package": "demo"}])

    def test_reads_an_empty_file_as_text_rather_than_unknown_compression(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "Packages"
            path.write_bytes(b"")
            with deb822.open_text(path) as source:
                self.assertEqual(list(deb822.stanzas(source)), [])

    def test_names_the_file_a_stray_byte_came_out_of(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "Packages"
            path.write_bytes(b"Package: \xff\n")
            with self.assertRaises(SystemExit) as failure, deb822.open_text(path) as source:
                list(deb822.stanzas(source))
        self.assertIn(f"{path}: is not UTF-8", str(failure.exception))


class TestPackageIndex(unittest.TestCase):
    def test_finds_the_index_where_the_suite_serves_it(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            index = root / "dists/testing/main/binary-amd64/Packages.xz"
            index.parent.mkdir(parents=True)
            index.touch()
            # The index of a retained generation is below `root`, but it must not count as a
            # second index of `root`.
            retained = root / "retained/0/dists/testing/main/binary-amd64/Packages.xz"
            retained.parent.mkdir(parents=True)
            retained.touch()
            self.assertEqual(deb822.package_index(root, "main"), index)
            self.assertEqual(deb822.package_index(root / "retained/0", "main"), retained)

    def test_rejects_none_or_several(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            with self.assertRaises(SystemExit):
                deb822.package_index(root, "main")
            for name in ("Packages.xz", "Packages.gz"):
                (root / "dists/testing/main/binary-amd64").mkdir(parents=True, exist_ok=True)
                (root / "dists/testing/main/binary-amd64" / name).touch()
            with self.assertRaises(SystemExit):
                deb822.package_index(root, "main")


if __name__ == "__main__":
    unittest.main()
