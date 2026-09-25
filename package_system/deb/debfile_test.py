# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for reading a deb's ar container.

buck test tine//package_system/deb:test
"""

import bz2
import compression.zstd
import gzip
import io
import lzma
import tarfile
import tempfile
import unittest
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

import debfile


def ar(*members: tuple[str, bytes], magic: bytes = debfile.MAGIC) -> bytes:
    out = bytearray(magic)
    for name, payload in members:
        out += f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(payload):<10}".encode() + b"`\n"
        out += payload + (b"\n" if len(payload) % 2 else b"")
    return bytes(out)


def add(archive: tarfile.TarFile, name: str, content: bytes = b"", mode: int = 0o644) -> None:
    """Add one member, a directory where the name says so."""
    info = tarfile.TarInfo(name)
    info.mode = mode
    if name.endswith("/"):
        info.type = tarfile.DIRTYPE
    else:
        info.size = len(content)
    archive.addfile(info, io.BytesIO(content))


def tar_bytes(
    build: Callable[[tarfile.TarFile], None], compress: Callable[[bytes], bytes] = lzma.compress
) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        build(archive)
    return compress(raw.getvalue())


def data_tar(*names: str, compress: Callable[[bytes], bytes] = lzma.compress) -> bytes:
    def build(archive: tarfile.TarFile) -> None:
        for name in names:
            add(archive, name, name.encode())

    return tar_bytes(build, compress)


def deb(
    root: Path,
    name: str = "demo.deb",
    *,
    data: bytes = b"",
    control: bytes = b"",
    suffix: str = ".xz",
    extra: bytes = b"",
) -> Path:
    """Assemble the three-member ar a `.deb` is."""
    path = root / name
    members = [
        ("debian-binary", b"2.0\n"),
        ("control.tar.xz", lzma.compress(control)),
        (f"data.tar{suffix}", data),
    ]
    if extra:
        members.append(("_trailing", extra))
    path.write_bytes(ar(*members))
    return path


class TestMembers(unittest.TestCase):
    def test_reads_names_offsets_and_sizes_in_order(self) -> None:
        raw = ar(("debian-binary", b"2.0\n"), ("data.tar.xz", b"payload"))
        found = list(debfile.members(io.BytesIO(raw), "demo.deb"))

        self.assertEqual([member.name for member in found], ["debian-binary", "data.tar.xz"])
        self.assertEqual([member.size for member in found], [4, 7])
        # The first payload is odd-length padded, so the second header does not start right after.
        self.assertEqual(raw[found[1].offset : found[1].offset + 7], b"payload")

    def test_accepts_a_gnu_slash_terminated_name(self) -> None:
        found = list(debfile.members(io.BytesIO(ar(("data.tar.gz/", b"ab"))), "demo.deb"))
        self.assertEqual(found[0].name, "data.tar.gz")

    def test_rejects_what_is_not_an_ar_archive(self) -> None:
        with self.assertRaises(SystemExit):
            list(debfile.members(io.BytesIO(ar(("data.tar", b"ab"), magic=b"<html>\n\n")), "demo.deb"))

    def test_rejects_a_truncated_or_unreadable_header(self) -> None:
        for raw in (
            debfile.MAGIC + b"short",
            debfile.MAGIC + b"x" * 58 + b"??",
            ar(("data.tar", b"ab"))[:-3],
        ):
            with self.subTest(raw=raw), self.assertRaises(SystemExit):
                list(debfile.members(io.BytesIO(raw), "demo.deb"))

    def test_rejects_the_gnu_long_name_table(self) -> None:
        with self.assertRaises(SystemExit):
            list(debfile.members(io.BytesIO(ar(("//", b"longname.tar/\n"))), "demo.deb"))


class TestOpenData(unittest.TestCase):
    def _names(self, package: Path) -> list[str]:
        with ExitStack() as stack:
            return [member.name for member in debfile.open_member(stack, package, debfile.DATA)]

    def test_reads_the_tree_under_every_compressor_debian_uses(self) -> None:
        for suffix, compress in (
            (".xz", lzma.compress),
            (".gz", gzip.compress),
            (".bz2", bz2.compress),
            (".zst", compression.zstd.compress),
            ("", lambda raw: raw),
        ):
            with tempfile.TemporaryDirectory() as scratch, self.subTest(suffix=suffix):
                data = data_tar("./usr/bin/demo", compress=compress)
                package = deb(Path(scratch), data=data, suffix=suffix)
                self.assertEqual(self._names(package), ["./usr/bin/demo"])

    def test_a_member_after_the_data_tar_does_not_reach_the_decompressor(self) -> None:
        # gzip and zstd read past their own stream to see whether another was concatenated on.
        with tempfile.TemporaryDirectory() as scratch:
            data = data_tar("./usr/bin/demo", compress=gzip.compress)
            package = deb(Path(scratch), data=data, suffix=".gz", extra=b"trailing member\n")
            self.assertEqual(self._names(package), ["./usr/bin/demo"])

    def test_rejects_a_package_carrying_no_data_member(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            package = Path(scratch) / "demo.deb"
            package.write_bytes(ar(("debian-binary", b"2.0\n"), ("control.tar.xz", lzma.compress(b""))))
            with ExitStack() as stack, self.assertRaises(SystemExit):
                debfile.open_member(stack, package, debfile.DATA)


if __name__ == "__main__":
    unittest.main()
