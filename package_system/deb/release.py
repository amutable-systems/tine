# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""A suite's Release: the clearsigned frame around it, and the files it states."""

import deb822
import util

import snapshotter
from href import relative_href

INRELEASE = "InRelease"

_BEGIN = b"-----BEGIN PGP SIGNED MESSAGE-----"
_SIGNATURE = b"-----BEGIN PGP SIGNATURE-----"
_END = b"-----END PGP SIGNATURE-----"


def unsigned(what: str, data: bytes) -> bytes:
    """The message a clearsigned file carries, read without verifying anything.

    RFC 9580's cleartext framing undone: the hash headers and the signature go, and dash-escaping
    and a line's trailing whitespace were never part of the message. Where the signature counts,
    the verifier has sqv do this.
    """
    lines = [line.rstrip(b" \t\r") for line in data.split(b"\n")]
    if lines[0] != _BEGIN:
        util.fail(f"{what}: not a clearsigned message")
    at = 1
    while at < len(lines) and lines[at]:
        at += 1
    try:
        stop = lines.index(_SIGNATURE, at)
        end = lines.index(_END, stop)
    except ValueError:
        util.fail(f"{what}: clearsigned message without a signature")
    if any(lines[end + 1 :]):
        util.fail(f"{what}: content after the signature")
    return b"\n".join(line[2:] if line.startswith(b"- ") else line for line in lines[at + 1 : stop])


def stanza(what: str, message: bytes) -> dict[str, str]:
    """The one stanza a Release consists of."""
    try:
        found = list(deb822.stanzas(message.decode("utf-8").splitlines()))
    except UnicodeDecodeError as error:
        util.fail(f"{what}: not UTF-8: {error}")
    if len(found) != 1:
        util.fail(f"{what}: holds {len(found)} stanzas, not one")
    return found[0]


def stated(rid: str, release: dict[str, str]) -> dict[str, tuple[str, int]]:
    """Every file the Release states a sha256 for, by its suite-relative path."""
    stated: dict[str, tuple[str, int]] = {}
    for number, line in enumerate(deb822.required(release, "sha256", f"{rid}: Release").splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 3:
            util.fail(f"{rid}: Release SHA256 line {number} is not a digest, size and path")
        digest, size, path = fields
        path = relative_href(rid, f"Release SHA256 line {number}", path)
        if path in stated:
            util.fail(f"{rid}: Release states {path} twice")
        stated[path] = (
            snapshotter.checksum(rid, f"Release entry {path}", digest),
            # A suite states every index it serves, and the ones this is not pinning are free to
            # be empty: Debian publishes a zero-length `Contents-udeb-all` in every component.
            deb822.integer(size, f"{rid}: Release entry {path}", minimum=0),
        )
    return stated
