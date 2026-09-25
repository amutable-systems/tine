# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Small, strict readers for Debian's Deb822 metadata."""

import io
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, TextIO, cast

import util
from util import MAGIC, decompressor

# What a Packages index is served as, most preferred first.
INDEX = "Packages"
INDEX_NAMES = (f"{INDEX}.xz", f"{INDEX}.gz", INDEX)


def package_index(directory: Path, what: str) -> Path:
    """Find the one Packages stream forming a materialized flat repository.

    A mirror serves the same index under several compressions at once, so whatever materializes a
    repository here has to pick one; two is an ambiguity this cannot resolve, not a mirror to read.
    """
    found = [directory / name for name in INDEX_NAMES if (directory / name).is_file()]
    if len(found) != 1:
        util.fail(f"{what}: expected exactly one Packages stream in {directory}, found {len(found)}")
    return found[0]


def stanzas(source: Iterable[str]) -> Iterator[dict[str, str]]:
    """Yield case-insensitive Deb822 stanzas with unfolded continuation lines.

    A stanza reader needs lines and nothing else, so a control file already in hand is as good a
    source as a stream being read off disk.
    """
    stanza: dict[str, str] = {}
    current: str | None = None
    for number, raw_line in enumerate(source, 1):
        # Trailing space goes the way APT drops it, so a stanza this reader and APT disagree about
        # is one neither accepts rather than one only this refuses.
        line = raw_line.rstrip(" \t\r\n")
        if not line:
            if stanza:
                yield stanza
                stanza = {}
                current = None
            continue
        if line[0] in " \t":
            if current is None:
                util.fail(f"Deb822 line {number}: continuation without a field")
            stanza[current] += "\n" + line[1:]
            continue

        name, separator, value = line.partition(":")
        if (
            not separator
            or not name
            or not name.isascii()
            or any(not (character.isalnum() or character == "-") for character in name)
        ):
            util.fail(f"Deb822 line {number}: invalid field {line!r}")
        current = name.lower()
        if current in stanza:
            util.fail(f"Deb822 line {number}: duplicate field {name!r}")
        stanza[current] = value.lstrip(" \t")

    if stanza:
        yield stanza


def integer(value: str, what: str, *, minimum: int) -> int:
    """Read a number Debian metadata states about itself.

    Spelled out rather than left to `int`, which also accepts a sign, surrounding space, non-ASCII
    digits and `1_000`; a size APT reads one way and this reads another is worth refusing.
    """
    if not value.isascii() or not value.isdigit():
        util.fail(f"{what} is not a decimal number: {value!r}")
    number = int(value)
    if number < minimum:
        util.fail(f"{what} is {number}, below the minimum {minimum}")
    return number


def required(stanza: dict[str, str], field: str, what: str) -> str:
    """Read a field a record is meaningless without."""
    value = stanza.get(field)
    if not value:
        util.fail(f"{what}: missing {field}")
    return value


class _Decoded:
    """A text stream whose decoding failures name the file they came out of.

    An index is read a line at a time, so a stray byte in one surfaces from wherever the caller
    happens to be iterating; every other malformed metadata here says what it was reading.
    """

    def __init__(self, path: Path, stream: TextIO) -> None:
        self._path = path
        self._stream = stream

    def __iter__(self) -> Iterator[str]:
        try:
            yield from self._stream
        except UnicodeDecodeError as error:
            util.fail(f"{self._path}: is not UTF-8: {error}")


@contextmanager
def open_text(path: Path) -> Iterator[Iterable[str]]:
    """Open an optionally compressed Deb822 file based on its bytes, not its suffix.

    Lines are all a stanza reader takes, so what comes back is an iterable of them rather than a
    file: that is what lets the decoding failures carry the name of the file they came out of.
    """
    with path.open("rb") as raw:
        magic = raw.read(MAGIC)
        raw.seek(0)
        opener = decompressor(magic)
        if opener is None:
            # A head that is empty or all whitespace names no compressor and no field either;
            # leave it to the parser, which can say what is actually wrong with the content.
            head = magic.lstrip()
            if head and not head[:1].isalnum():
                util.fail(f"{path}: unsupported compression (magic {magic.hex()})")
            with io.TextIOWrapper(raw, encoding="utf-8") as text:
                yield _Decoded(path, text)
            return
        with opener(raw) as compressed:
            with io.TextIOWrapper(cast(BinaryIO, compressed), encoding="utf-8") as text:
                yield _Decoded(path, text)
