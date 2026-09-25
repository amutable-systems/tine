#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build the keyring that verifies a Debian repository's signed Release.

The archive's own keys sign the Release, so there is no trust to compute: the keyring is the
declared certificates in the binary form sqv reads, each checked to be the key it is declared
as, and the time the repository's snapshot was published, for the verifier to judge them as of.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import openpgp
import specs
import util


class Spec(TypedDict):
    # The declared fingerprint of each key file.
    keys: dict[str, str]
    out: str
    # When the repository's snapshot was published, ISO 8601 in UTC, or None to judge as of now.
    time: str | None


def _certificate(fingerprint: str, file: str) -> bytes:
    try:
        binary = openpgp.dearmor(Path(file).read_text(encoding="ascii"))
        found = sorted(set(openpgp.primary_fingerprints(binary)))
    except (UnicodeDecodeError, openpgp.Malformed) as error:
        util.fail(f"keyring: {file} is not a public key file: {error}")
    if found != [fingerprint.upper()]:
        util.fail(f"keyring: {file} holds {found}, not the declared key {fingerprint}")
    return binary


def instant(time: str) -> str:
    """A pinned time as sqv takes it: UTC, to the second."""
    try:
        when = datetime.fromisoformat(time)
    except ValueError:
        util.fail(f"keyring: {time!r} is not an ISO 8601 time")
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def keyring(spec: Spec) -> None:
    """Write the declared certificates as one keyring, refusing any key but the declared ones."""
    out = Path(spec["out"])
    out.mkdir(parents=True)
    certificates = b"".join(
        _certificate(fingerprint, file) for fingerprint, file in sorted(spec["keys"].items())
    )
    (out / openpgp.KEYRING_FILE).write_bytes(certificates)
    if spec["time"] is not None:
        (out / openpgp.TIME_FILE).write_text(instant(spec["time"]) + "\n")
    print(f"keyring: holds {len(spec['keys'])} declared key(s)", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    keyring(specs.parse(Spec, "keyring", argv))


if __name__ == "__main__":
    main()
