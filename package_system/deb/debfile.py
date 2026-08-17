# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Frame a Debian binary package's `ar` container, and name where dpkg keeps what it installs.

A `.deb` is an `ar` archive of three members in order: `debian-binary` naming the format version,
a control tar, and the data tar carrying the tree the package installs. Reading that framing here
is what lets the bootstrap extractor unpack a package before there is a dpkg to do it.
"""

import tarfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import IO, BinaryIO, NamedTuple, cast

import util

MAGIC = b"!<arch>\n"
# Where dpkg records what it installed. Keep in sync with the `database_paths` in this package's
# BUCK file, which Starlark cannot read from here.
ADMINDIR = "var/lib/dpkg"
DATA = "data.tar"
CONTROL = "control.tar"
# Every field of an ar header is fixed-width ASCII, and a member is padded to an even offset.
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
    # GNU ar terminates a name with a slash and pads to width; dpkg pads a plain name.
    name = name.rstrip().removesuffix("/")
    if not name or name.startswith("/"):
        # `/` and `//` name GNU's long-name table, which a deb has no names long enough to need.
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
        # Read the way `strtoul` reads it and this project's own metadata numbers: `int` would
        # also take a sign, surrounding space and `1_0`, which is a different number here.
        stated = header[_SIZE].decode("ascii", "replace").strip()
        if not stated.isascii() or not stated.isdigit():
            util.fail(f"{where} has an unreadable size {bytes(header[_SIZE])!r}")
        size = int(stated)
        yield Member(_name(where, header[_NAME]), offset + _HEADER, size)
        # A member's payload is padded to an even offset, and the padding is not its own.
        offset += _HEADER + size + size % 2
        source.seek(offset)


class _Region:
    """One member's bytes and no more.

    A decompressor reads past the end of its own stream to see whether another was concatenated
    onto it, so handing one the whole file lets a `data.tar.gz` or `data.tar.zst` fail on whatever
    member happens to follow it. Only `read` is needed: nothing here decompresses by seeking.
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


def open_member(stack: ExitStack, package: Path, prefix: str) -> tarfile.TarFile:
    """Open one of a package's tar members, whatever it was compressed with."""
    what = package.name
    raw = stack.enter_context(package.open("rb"))
    data = next((member for member in members(raw, what) if member.name.startswith(prefix)), None)
    if data is None:
        util.fail(f"{what} carries no {prefix} member")

    raw.seek(data.offset)
    opener = util.decompressor(raw.read(util.MAGIC))
    region = _Region(raw, data)
    # A data tar may be stored uncompressed, which names no compressor at all.
    stream = region if opener is None else stack.enter_context(opener(cast(IO[bytes], region)))
    # A decompressor is a file object typeshed does not declare as one, and `r|` reads the tar as
    # a stream, so what tarfile actually needs of either is `read`.
    return stack.enter_context(tarfile.open(fileobj=cast(IO[bytes], stream), mode="r|"))


def control(package: Path) -> str:
    """Read the stanza describing a package, as its own control member states it."""
    with ExitStack() as stack:
        archive = open_member(stack, package, CONTROL)
        for member in archive:
            if member.name.removeprefix("./") != "control":
                continue
            source = archive.extractfile(member)
            if source is None:
                break
            return source.read().decode("utf-8")
    util.fail(f"{package.name} carries no control stanza")


def unpack(package: Path, dest: Path, keep: Callable[[str], bool] | None = None) -> int:
    """Write the tree a package installs into `dest`, returning the number of entries written.

    `keep` is asked about the absolute path a member installs to, not the name the archive gives it.
    """
    written = 0
    dropped = []
    with ExitStack() as stack:
        archive = open_member(stack, package, DATA)
        for member in archive:
            # A data tar names its own root, which is the destination rather than part of the
            # tree any package installs.
            if member.name.rstrip("/") in ("", "."):
                continue
            archive.extract(member, dest, filter="tar")
            written += 1
            # The filter strips a leading slash into a member of its own, which `extract` does not
            # hand back, so the name is normalized here too rather than asked about as it came.
            installed = "/" + member.name.removeprefix("./").lstrip("/")
            if keep is not None and not keep(installed):
                dropped.append(dest / installed.lstrip("/"))

    # Everything lands and what is unwanted goes afterwards, rather than being skipped as it
    # streams past: a kept member may be a hardlink to a dropped one, and a tar read as a stream
    # cannot seek back to extract a target it was told to skip. Removing it afterwards leaves the
    # link holding the content, which is what dpkg's own filtering does.
    # Deepest first, so a directory the rules also dropped is empty by the time it is reached, and
    # it only goes when nothing a rule kept is left in it.
    for path in sorted(dropped, key=lambda path: len(path.parts), reverse=True):
        # A dropped symlink goes rather than whatever it points at, and Debian ships plenty that
        # point outside the tree.
        if path.is_dir() and not path.is_symlink():
            if any(path.iterdir()):
                continue
            path.rmdir()
        else:
            path.unlink(missing_ok=True)
        written -= 1
        # tarfile creates the parents of a member whether or not the archive named them, so a
        # dropped tree can be left standing empty by a package that named none of its directories.
        for parent in path.parents:
            if parent == dest or parent.is_symlink() or not parent.is_dir():
                break
            if any(parent.iterdir()):
                break
            parent.rmdir()
    return written
