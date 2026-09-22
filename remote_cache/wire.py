# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Just enough of the RE-API protobuf wire format to serve a cache.

Every field this shim reads or writes is a varint or a length-delimited value: strings, bytes,
integers, booleans, nested messages, and one repeated enum that proto3 packs into a single
length-delimited field. No floats, no zigzag, no maps. That is the whole format we need, which is
why it is cheaper to encode by hand than to carry a generated runtime (which are not properly typed either).

Reading is deliberately forgiving. A client will send fields we do not model, and a newer one will
send fields that did not exist when this was written; both have to pass through without complaint.
Writing is proto3: a field at its default value is left out entirely, and an empty nested message
is still written.
"""

from collections.abc import Iterator

VARINT = 0
FIXED64 = 1
LENGTH = 2
FIXED32 = 5


def _varint(data: bytes, at: int) -> tuple[int, int]:
    """One varint, and where it ended."""
    value = 0
    shift = 0
    while True:
        if at >= len(data):
            raise ValueError("varint runs past the end of the message")
        if shift > 63:
            raise ValueError("varint is longer than 64 bits")
        byte = data[at]
        value |= (byte & 0x7F) << shift
        at += 1
        if not byte & 0x80:
            return value, at
        shift += 7


def fields(data: bytes) -> Iterator[tuple[int, int | bytes]]:
    """Every field as its number and value: varints as `int`, length-delimited as `bytes`.

    Fixed-width fields are skipped, since nothing here uses one. Groups are refused: they were
    removed from proto3, so one arriving means this is not the message we think it is.
    """
    at = 0
    while at < len(data):
        tag, at = _varint(data, at)
        number, kind = tag >> 3, tag & 0x07
        if number == 0:
            raise ValueError("field number 0 is not valid")
        if kind == VARINT:
            value, at = _varint(data, at)
            yield number, value
        elif kind == LENGTH:
            length, at = _varint(data, at)
            if at + length > len(data):
                raise ValueError("length-delimited field runs past the end of the message")
            yield number, data[at : at + length]
            at += length
        elif kind == FIXED64:
            at += 8
        elif kind == FIXED32:
            at += 4
        else:
            raise ValueError(f"field {number} has wire type {kind}, which proto3 does not use")
        if at > len(data):
            raise ValueError("field runs past the end of the message")


class Message:
    """A parsed message, its fields reachable by number.

    Repeated fields keep their order. For a singular field the last one wins, which is what proto3
    says happens when a field arrives twice.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.by_number: dict[int, list[int | bytes]] = {}
        for number, value in fields(data):
            self.by_number.setdefault(number, []).append(value)

    # The two wire shapes, kept apart: a field number is one kind or the other, and a value of the
    # wrong kind is a writer we do not understand rather than something to coerce.
    def _ints(self, number: int) -> list[int]:
        return [one for one in self.by_number.get(number, []) if isinstance(one, int)]

    def blobs(self, number: int) -> list[bytes]:
        return [one for one in self.by_number.get(number, []) if isinstance(one, bytes)]

    def integer(self, number: int, default: int = 0) -> int:
        return (self._ints(number) or [default])[-1]

    def flag(self, number: int) -> bool:
        return bool(self.integer(number))

    def blob(self, number: int, default: bytes = b"") -> bytes:
        return (self.blobs(number) or [default])[-1]

    def text(self, number: int, default: str = "") -> str:
        blob = self.blob(number)
        return blob.decode(errors="replace") if blob else default

    def child(self, number: int) -> Message | None:
        return Message(values[-1]) if (values := self.blobs(number)) else None

    def children(self, number: int) -> list[Message]:
        return [Message(one) for one in self.blobs(number)]

    def numbers(self, number: int) -> list[int]:
        """A repeated numeric field, however it was written.

        proto3 packs these into one length-delimited field, but a parser has to take a tag per
        value too, because that is what older writers emit and both remain legal.
        """
        return self._ints(number) + [one for packed in self.blobs(number) for one in unpack_varints(packed)]


def unpack_varints(data: bytes) -> list[int]:
    """Every varint in a packed repeated field."""
    out = []
    at = 0
    while at < len(data):
        value, at = _varint(data, at)
        out.append(value)
    return out


def _tag(number: int, kind: int) -> bytes:
    return _write_varint(number << 3 | kind)


def _write_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError(f"nothing here writes a negative varint, got {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def varint(number: int, value: int) -> bytes:
    """A varint field, left out when it is zero, as proto3 does."""
    return _tag(number, VARINT) + _write_varint(value) if value else b""


def flag(number: int, value: bool) -> bytes:
    return varint(number, int(value))


def blob(number: int, value: bytes) -> bytes:
    """A bytes field, left out when it is empty."""
    return _tag(number, LENGTH) + _write_varint(len(value)) + value if value else b""


def text(number: int, value: str) -> bytes:
    return blob(number, value.encode())


def packed(number: int, values: tuple[int, ...]) -> bytes:
    """A repeated numeric field the way proto3 writes one: all the varints in one field."""
    return blob(number, b"".join(_write_varint(one) for one in values))


def submessage(number: int, body: bytes) -> bytes:
    """A nested message, written even when its body is empty: the tag is what says it is there."""
    return _tag(number, LENGTH) + _write_varint(len(body)) + body
