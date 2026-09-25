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
            "Request: EDSP 0.5\nArchitectures: amd64\n\n"
            "Package: demo\nAPT-Release:\n o=Debian,l=main\n n=trixie\n\n"
        )
        self.assertEqual(
            list(deb822.stanzas(source)),
            [
                {"request": "EDSP 0.5", "architectures": "amd64"},
                {"package": "demo", "apt-release": "\no=Debian,l=main\nn=trixie"},
            ],
        )

    def test_rejects_malformed_input(self) -> None:
        for content in (" continuation\n", "Package demo\n", "Package: one\npackage: two\n"):
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


if __name__ == "__main__":
    unittest.main()
