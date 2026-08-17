# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for unpacking Debian packages without dpkg.

buck test tine//package_system/deb:test
"""

import tarfile
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

import debfile
import debfile_test
from debfile_test import add

import extractor


def deb(root: Path, name: str, build: Callable[[tarfile.TarFile], None]) -> Path:
    return debfile_test.deb(root, name, data=debfile_test.tar_bytes(build))


class TestExtract(unittest.TestCase):
    def test_writes_the_tree_a_package_installs(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            add(archive, "./")
            add(archive, "./usr/", mode=0o755)
            add(archive, "./usr/bin/", mode=0o755)
            add(archive, "./usr/bin/demo", b"#!/bin/sh\n", mode=0o755)
            add(archive, "./etc/demo.conf", b"key = value\n")

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            dest = root / "out"
            # The root entry is the destination, not part of the tree, so it is not counted.
            self.assertEqual(debfile.unpack(deb(root, "demo.deb", build), dest), 4)
            self.assertEqual((dest / "usr/bin/demo").read_bytes(), b"#!/bin/sh\n")
            self.assertEqual((dest / "usr/bin/demo").stat().st_mode & 0o777, 0o755)
            self.assertEqual((dest / "etc/demo.conf").read_bytes(), b"key = value\n")

    def test_reproduces_hardlinks_and_symlinks_from_a_streamed_tar(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            add(archive, "./usr/bin/demo", b"#!/bin/sh\n", mode=0o755)
            link = tarfile.TarInfo("./usr/bin/hard")
            link.type, link.linkname = tarfile.LNKTYPE, "./usr/bin/demo"
            archive.addfile(link)
            symlink = tarfile.TarInfo("./usr/bin/soft")
            symlink.type, symlink.linkname = tarfile.SYMTYPE, "demo"
            archive.addfile(symlink)

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            dest = root / "out"
            debfile.unpack(deb(root, "demo.deb", build), dest)

            # A tar read as a stream cannot seek back, so a hardlink is made from what landed.
            self.assertEqual((dest / "usr/bin/hard").read_bytes(), b"#!/bin/sh\n")
            self.assertEqual((dest / "usr/bin/demo").stat().st_ino, (dest / "usr/bin/hard").stat().st_ino)
            self.assertTrue((dest / "usr/bin/soft").is_symlink())

    def test_refuses_a_member_that_would_escape_the_destination(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            add(archive, "../escaped", b"nope\n")

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            with self.assertRaises(tarfile.OutsideDestinationError):
                debfile.unpack(deb(root, "demo.deb", build), root / "out")
            self.assertFalse((root / "escaped").exists())

    def test_keeps_only_what_the_caller_asks_for(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            add(archive, "./usr/bin/demo", b"x")
            add(archive, "./usr/share/man/man1/demo.1", b"x")

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            dest = root / "out"
            written = debfile.unpack(
                deb(root, "demo.deb", build), dest, lambda path: not path.startswith("/usr/share/man/")
            )

            self.assertEqual(written, 1)
            self.assertTrue((dest / "usr/bin/demo").is_file())
            self.assertFalse((dest / "usr/share/man").exists())

    def test_drops_a_symlink_rather_than_what_it_points_at(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            add(archive, "./usr/share/doc/demo/changelog.gz", b"log\n")
            # The convention a Debian package documents a sibling's files with.
            symlink = tarfile.TarInfo("./usr/share/doc/demo-common")
            symlink.type, symlink.linkname = tarfile.SYMTYPE, "demo"
            archive.addfile(symlink)

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            dest = root / "out"
            debfile.unpack(
                deb(root, "demo.deb", build), dest, lambda path: path != "/usr/share/doc/demo-common"
            )

            self.assertFalse((dest / "usr/share/doc/demo-common").is_symlink())
            self.assertTrue((dest / "usr/share/doc/demo/changelog.gz").is_file())

    def test_expands_a_directory_of_packages(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            add(archive, "./usr/bin/demo", b"x")

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            packages = root / "packages"
            packages.mkdir()
            deb(packages, "b.deb", build)
            deb(packages, "a.deb", build)
            self.assertEqual([path.name for path in extractor.expand([str(packages)])], ["a.deb", "b.deb"])


if __name__ == "__main__":
    unittest.main()
