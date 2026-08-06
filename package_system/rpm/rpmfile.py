"""Frame RPM headers, access tags, and stream payload decompression.

This stdlib-only path lets the bootstrap extractor read RPMs without the RPM stack.
"""

import shutil
import struct
from collections.abc import Buffer
from typing import BinaryIO, NamedTuple

import util

MAGIC = b"\x8e\xad\xe8\x01"
LEAD = 96
CPIO_MAGIC = b"070701"  # newc — an already-uncompressed payload passes through


class Header(NamedTuple):
    """An RPM header and its tag offsets into the value store."""

    start: int
    store: int
    end: int
    tags: dict[int, tuple[int, int, int]]

    def loc(self, tag: int) -> tuple[int, int]:
        """Absolute (offset, count) of `tag`'s value in the file."""
        _typ, off, count = self.tags[tag]
        return self.store + off, count


def _header(view: memoryview, off: int) -> Header:
    if bytes(view[off : off + 4]) != MAGIC:
        raise SystemExit(f"bad rpm header magic at {off}: {bytes(view[off : off + 4]).hex()}")
    nindex, nbytes = struct.unpack(">II", view[off + 8 : off + 16])
    index = off + 16
    store = index + 16 * nindex
    tags = {}
    for i in range(nindex):
        tag, typ, offset, count = struct.unpack(">IIII", view[index + 16 * i : index + 16 * i + 16])
        tags[tag] = (typ, offset, count)
    return Header(off, store, store + nbytes, tags)


def headers(data: Buffer) -> tuple[Header, Header]:
    """Return the signature and main headers from a buffer-like RPM."""
    view = memoryview(data)
    sig = _header(view, LEAD)
    main = _header(view, sig.end + (-sig.end % 8))  # the signature header is padded to 8
    return sig, main


def decompress_stream(source: BinaryIO, output: BinaryIO) -> None:
    """Stream one payload from `source`'s current offset into an uncompressed cpio."""
    start = source.tell()
    magic = source.read(util.MAGIC)
    source.seek(start)
    open_compressed = util.decompressor(magic)
    if open_compressed is None:
        if not magic.startswith(CPIO_MAGIC):
            raise SystemExit(f"unknown payload compressor (magic {magic.hex()})")
        shutil.copyfileobj(source, output, length=1024 * 1024)
        return
    with open_compressed(source) as reader:
        shutil.copyfileobj(reader, output, length=1024 * 1024)
