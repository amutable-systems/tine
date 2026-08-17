# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Read the `ar` container of a Debian binary package.

A `.deb` is an `ar` archive with three members in this order: `debian-binary` with the format
version, a control tar, and a data tar with the files that the package installs. `extract.py` uses
this module to unpack packages into a root that has no dpkg yet.
"""

import tarfile
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import IO, BinaryIO, NamedTuple, cast

import util

MAGIC = b"!<arch>\n"
# The directory of the dpkg database. Keep in sync with `database_paths` in the BUCK file of this
# package.
ADMINDIR = "var/lib/dpkg"
DATA = "data.tar"
# An ar header has 60 bytes of fixed-width ASCII fields.
_HEADER = 60
_SIZE = slice(48, 58)
_NAME = slice(0, 16)
_TRAILER = slice(58, 60)


class Member(NamedTuple):
    name: str
    offset: int
    size: int


def _name(what: str, raw: bytes) -> str:
    try:
        name = raw.decode("ascii")
    except UnicodeDecodeError:
        util.fail(f"{what} has a non-ASCII name {raw!r}")
    # GNU ar ends a name with a slash and pads it with spaces. dpkg pads the name without a slash.
    name = name.rstrip().removesuffix("/")
    if not name or name.startswith("/"):
        # GNU ar names its symbol table `/` and its table of long names `//`. A deb needs neither.
        util.fail(f"{what} has unsupported name {name!r}")
    return name


def members(source: BinaryIO, what: str) -> Iterator[Member]:
    """Yield each member of an `ar` archive, in the order it was written."""
    if source.read(len(MAGIC)) != MAGIC:
        util.fail(f"{what} is not an ar archive")
    offset = len(MAGIC)
    while header := source.read(_HEADER):
        where = f"{what} member at {offset}"
        if len(header) != _HEADER or header[_TRAILER] != b"`\n":
            util.fail(f"{where} has no header")
        # `int()` alone also accepts a sign and `1_0`. Accept only decimal digits, as
        # `deb822.integer()` does.
        stated = header[_SIZE].decode("ascii", "replace").strip()
        if not stated.isascii() or not stated.isdigit():
            util.fail(f"{where} has an unreadable size {bytes(header[_SIZE])!r}")
        size = int(stated)
        yield Member(_name(where, header[_NAME]), offset + _HEADER, size)
        # ar pads the data of a member to an even length. The size in the header excludes the
        # padding.
        offset += _HEADER + size + size % 2
        source.seek(offset)


class _Region:
    """A reader that returns the bytes of one member and then reports the end of the file.

    The gzip and zstd decompressors read past the end of their stream to look for a concatenated
    stream. With the whole file as input, they would fail on the member that follows the data tar.
    The class has only `read()` because no caller seeks.
    """

    def __init__(self, source: BinaryIO, member: Member) -> None:
        self._source = source
        self._remaining = member.size
        source.seek(member.offset)

    def read(self, size: int = -1) -> bytes:
        wanted = self._remaining if size < 0 else min(size, self._remaining)
        chunk = self._source.read(wanted)
        self._remaining -= len(chunk)
        return chunk


def open_data(stack: ExitStack, package: Path) -> tarfile.TarFile:
    """Open the data tar of a package, and detect its compression from its first bytes."""
    what = package.name
    raw = stack.enter_context(package.open("rb"))
    data = next((member for member in members(raw, what) if member.name.startswith(DATA)), None)
    if data is None:
        util.fail(f"{what} carries no {DATA} member")

    raw.seek(data.offset)
    opener = util.decompressor(raw.read(util.MAGIC))
    region = _Region(raw, data)
    # `opener` is None for an uncompressed `data.tar`, which dpkg also accepts.
    stream = region if opener is None else stack.enter_context(opener(cast(IO[bytes], region)))
    # Both casts are safe: mode `r|` reads the tar as a stream, so tarfile only calls `read()`.
    return stack.enter_context(tarfile.open(fileobj=cast(IO[bytes], stream), mode="r|"))


def unpack(package: Path, dest: Path) -> int:
    """Extract the files of a package into `dest`, and return the number of entries written."""
    written = 0
    with ExitStack() as stack:
        archive = open_data(stack, package)
        for member in archive:
            # The data tar has an entry for its root directory `./`, which is `dest` itself.
            if member.name.rstrip("/") in ("", "."):
                continue
            archive.extract(member, dest, filter="tar")
            # `os.symlink()` fails when a directory is at the path of the symlink. tarfile then
            # assumes a platform without symlinks and reports nothing.
            if member.issym() and not (dest / member.name).is_symlink():
                util.fail(f"{package.name}: {member.name} is a link, and a directory is in its place")
            written += 1
    return written
