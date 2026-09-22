# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Frame RPM headers and stream payload decompression.

This stdlib-only path lets the bootstrap extractor read RPMs without the RPM stack.
"""

import shutil
import struct
from collections.abc import Buffer
from typing import BinaryIO

import util

MAGIC = b"\x8e\xad\xe8\x01"
LEAD = 96
CPIO_MAGIC = b"070701"  # newc — an already-uncompressed payload passes through


def _header_end(view: memoryview, off: int) -> int:
    if bytes(view[off : off + 4]) != MAGIC:
        util.fail(f"bad rpm header magic at {off}: {bytes(view[off : off + 4]).hex()}")
    nindex, nbytes = struct.unpack(">II", view[off + 8 : off + 16])
    return off + 16 + 16 * nindex + nbytes


def payload_offset(data: Buffer) -> int:
    """Return where the payload starts in a buffer-like RPM, past the lead and both headers."""
    view = memoryview(data)
    signature = _header_end(view, LEAD)
    return _header_end(view, signature + (-signature % 8))  # the signature header is padded to 8


def decompress_stream(source: BinaryIO, output: BinaryIO) -> None:
    """Stream one payload from `source`'s current offset into an uncompressed cpio."""
    start = source.tell()
    magic = source.read(util.MAGIC)
    source.seek(start)
    open_compressed = util.decompressor(magic)
    if open_compressed is None:
        if not magic.startswith(CPIO_MAGIC):
            util.fail(f"unknown payload compressor (magic {magic.hex()})")
        shutil.copyfileobj(source, output, length=1024 * 1024)
        return
    with open_compressed(source) as reader:
        shutil.copyfileobj(reader, output, length=1024 * 1024)
