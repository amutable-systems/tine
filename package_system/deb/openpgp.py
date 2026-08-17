# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The little of OpenPGP a keyring of declared certificates takes: armor, packet framing, fingerprints."""

import base64
import binascii
import hashlib
from collections.abc import Iterator

ARMOR_HEADER = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
ARMOR_FOOTER = "-----END PGP PUBLIC KEY BLOCK-----"

# What a built keyring consists of: the declared certificates in the binary form sqv reads, and
# the time to judge them as of, absent for a rolling repository.
KEYRING_FILE = "keyring.pgp"
TIME_FILE = "time"

# The packet tags a public key file consists of (RFC 9580 section 5): a secret key is refused by
# name, anything else as not belonging.
_PUBLIC_KEY = 6
_SECRET = (5, 7)
_CERTIFICATE = (2, 6, 13, 14, 17)


class Malformed(ValueError):
    """The bytes are not the public key file they are declared to be."""


def dearmor(text: str) -> bytes:
    """The binary form of the public key blocks in an armored file, concatenated as a keyring is.

    The CRC is not checked: the primary key's fingerprint is, and a damaged subkey or certification
    can only fail to verify.
    """
    lines = [line.strip() for line in text.splitlines()]
    out = bytearray()
    at = 0
    while at < len(lines):
        if not lines[at]:
            at += 1
            continue
        if lines[at] != ARMOR_HEADER:
            raise Malformed(f"line {at + 1}: expected {ARMOR_HEADER}, found {lines[at]!r}")
        at += 1
        while at < len(lines) and ": " in lines[at]:
            at += 1
        if at < len(lines) and not lines[at]:
            at += 1
        body = []
        while at < len(lines) and lines[at] != ARMOR_FOOTER:
            if not lines[at].startswith("="):
                body.append(lines[at])
            at += 1
        if at == len(lines):
            raise Malformed(f"missing {ARMOR_FOOTER}")
        at += 1
        if not body:
            raise Malformed("an empty key block")
        try:
            out += base64.b64decode("".join(body), validate=True)
        except binascii.Error as error:
            raise Malformed(f"the armored data is not base64: {error}") from None
    if not out:
        raise Malformed(f"no {ARMOR_HEADER}")
    return bytes(out)


def packets(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Frame a binary OpenPGP stream into its packets, as (tag, body)."""
    at = 0

    def take(count: int) -> bytes:
        nonlocal at
        if at + count > len(data):
            raise Malformed(f"offset {at}: truncated packet")
        at += count
        return data[at - count : at]

    while at < len(data):
        (first,) = take(1)
        if not first & 0x80:
            raise Malformed(f"offset {at - 1}: not a packet header")
        if first & 0x40:
            tag = first & 0x3F
            (length,) = take(1)
            if 192 <= length < 224:
                length = ((length - 192) << 8) + take(1)[0] + 192
            elif length == 255:
                length = int.from_bytes(take(4), "big")
            elif length >= 224:
                raise Malformed(f"offset {at}: partial body lengths frame a stream, not a key")
        else:
            tag = (first >> 2) & 0x0F
            kind = first & 0x03
            if kind == 3:
                raise Malformed(f"offset {at}: a packet of indeterminate length")
            length = int.from_bytes(take(1 << kind), "big")
        yield tag, take(length)


def fingerprint(body: bytes) -> str:
    """The fingerprint of a public key packet, upper-case hex as gpg lists it."""
    version = body[0] if body else 0
    try:
        if version == 4:
            digest = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body)
        elif version == 6:
            digest = hashlib.sha256(b"\x9b" + len(body).to_bytes(4, "big") + body)
        else:
            raise Malformed(f"a version {version} key")
    except OverflowError:
        # A v4 fingerprint counts the body in two bytes, so one this long is not a key packet.
        raise Malformed(f"a version {version} key of {len(body)} bytes") from None
    return digest.hexdigest().upper()


def primary_fingerprints(data: bytes) -> list[str]:
    """The primary key fingerprint of each certificate in a binary keyring, in order."""
    found = []
    for tag, body in packets(data):
        if tag in _SECRET:
            raise Malformed("carries a secret key, which no public key file does")
        if tag not in _CERTIFICATE:
            raise Malformed(f"a packet of tag {tag} belongs in no public key file")
        if tag == _PUBLIC_KEY:
            found.append(fingerprint(body))
    if not found:
        raise Malformed("holds no key")
    return found
