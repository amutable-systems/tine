"""rpm-file primitives: header framing, tag access, and streaming payload decompression.

Stdlib only (compression.zstd needs host python >= 3.14). Used by extract.py (payload → files) to
read a downloaded rpm without the real rpm stack.

An rpm on disk is: a 96-byte legacy lead, a signature header (padded to 8), the main header,
then the payload. Each header is `\x8e\xad\xe8\x01` + reserved + nindex + nbytes, an index of
16-byte entries `(tag, type, store-offset, count)`, then the value store. We frame the headers and
locate tag values without parsing the whole file."""

import compression.zstd
import gzip
import lzma
import shutil
import struct
from collections.abc import Buffer
from typing import BinaryIO, NamedTuple

MAGIC = b"\x8e\xad\xe8\x01"
LEAD = 96
CPIO_MAGIC = b"070701"  # newc — an already-uncompressed payload passes through


class Header(NamedTuple):
    """One rpm header section. `start` is the intro magic; `store`..`end` the value store.

    `tags` maps a tag to `(type, offset-within-store, count)`; `loc` turns that into the value's
    absolute position, so a caller can read or overwrite it in place."""

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
    """The signature + main header sections; the payload begins at `main.end`. `data` is anything
    buffer-like (bytes or an mmap of the rpm), so a caller can frame without reading the whole file."""
    view = memoryview(data)
    sig = _header(view, LEAD)
    main = _header(view, sig.end + (-sig.end % 8))  # the signature header is padded to 8
    return sig, main


def decompress_stream(source: BinaryIO, output: BinaryIO) -> None:
    """Stream one payload from `source`'s current offset into an uncompressed cpio."""
    start = source.tell()
    magic = source.read(6)
    source.seek(start)
    if magic[:4] == b"\x28\xb5\x2f\xfd":
        reader = compression.zstd.ZstdFile(source, mode="rb")
    elif magic == b"\xfd7zXZ\x00":
        reader = lzma.LZMAFile(source, mode="rb")
    elif magic[:2] == b"\x1f\x8b":
        reader = gzip.GzipFile(fileobj=source, mode="rb")
    elif magic == CPIO_MAGIC:
        shutil.copyfileobj(source, output, length=1024 * 1024)
        return
    else:
        raise SystemExit(f"unknown payload compressor (magic {magic.hex()})")
    with reader:
        shutil.copyfileobj(reader, output, length=1024 * 1024)
