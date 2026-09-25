# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for indexing a repository of locally built packages.

buck test tine//package_system/deb:test
"""

import lzma
import tempfile
import unittest
from pathlib import Path

import debfile_test

import index

CONTROL = (
    "Package: demo\n"
    "Version: 1.0-1\n"
    "Architecture: amd64\n"
    "Maintainer: Nobody <nobody@example.invalid>\n"
    "Description: a demo\n"
    " folded over\n"
    " several lines\n"
)


def deb(root: Path, name: str, control: str = CONTROL) -> Path:
    stanza = debfile_test.tar_bytes(
        lambda archive: debfile_test.add(archive, "./control", control.encode()), lambda raw: raw
    )
    return debfile_test.deb(root, name, control=stanza, data=lzma.compress(b""))


class TestStanza(unittest.TestCase):
    def test_copies_the_control_stanza_verbatim_and_adds_the_transport(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            package = deb(Path(scratch), "demo.deb")
            written = index.stanza("0/demo.deb", package)

            # A folded Description keeps its continuation lines, and field names their spelling.
            self.assertIn("Description: a demo\n folded over\n several lines\n", written)
            self.assertIn("Filename: 0/demo.deb\n", written)
            self.assertIn(f"Size: {package.stat().st_size}\n", written)
            self.assertTrue(written.endswith("\n\n"))

    def test_rejects_a_control_that_states_what_the_index_owns(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            package = deb(Path(scratch), "demo.deb", CONTROL + "Filename: elsewhere.deb\n")
            with self.assertRaises(SystemExit):
                index.stanza("0/demo.deb", package)


class TestIndex(unittest.TestCase):
    def test_publishes_every_package_beside_one_index(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            out = root / "repo"
            entries = [("0/b.deb", deb(root, "b.deb")), ("0/a.deb", deb(root, "a.deb"))]
            index.index(entries, out)

            self.assertTrue((out / "0/a.deb").is_file())
            self.assertTrue((out / "0/b.deb").is_file())
            written = (out / "Packages").read_text(encoding="utf-8")
            # Sorted, so an identical repository indexes to identical bytes.
            self.assertLess(written.index("Filename: 0/a.deb"), written.index("Filename: 0/b.deb"))

    def test_refuses_an_empty_repository(self) -> None:
        with tempfile.TemporaryDirectory() as scratch, self.assertRaises(SystemExit):
            index.index([], Path(scratch) / "repo")


if __name__ == "__main__":
    unittest.main()
