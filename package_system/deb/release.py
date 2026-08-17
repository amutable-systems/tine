# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Read the Release of a suite: the names of the suite and the files that the Release lists."""

import deb822
import util

import snapshotter
from href import relative_href

RELEASE = "Release"
INRELEASE = "InRelease"


def stanza(what: str, message: bytes) -> dict[str, str]:
    """Return the single stanza of a Release."""
    try:
        found = list(deb822.stanzas(message.decode("utf-8").splitlines()))
    except UnicodeDecodeError as error:
        util.fail(f"{what}: not UTF-8: {error}")
    if len(found) != 1:
        util.fail(f"{what}: holds {len(found)} stanzas, not one")
    return found[0]


def stated(rid: str, release: dict[str, str]) -> dict[str, tuple[str, int]]:
    """Return the checksum and size of every file in the `SHA256` field, keyed by its path in the suite."""
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
            # The field lists every index of the suite, and some of them are empty: Debian
            # publishes a `Contents-udeb-all` of zero bytes in every component.
            deb822.integer(size, f"{rid}: Release entry {path}", minimum=0),
        )
    return stated


def named(rid: str, release: dict[str, str], suite: str) -> None:
    """Fail unless the Release names `suite` as its suite or as its codename.

    An archive signs the Release of every suite with the same keys, so a valid signature does not
    prove that the Release belongs to `suite`. A mirror serves a suite under both names.
    """
    names = [release[field] for field in ("suite", "codename") if field in release]
    if suite not in names:
        util.fail(f"{rid}: Release is of {' or '.join(names) or 'no suite'}, not {suite!r}")
