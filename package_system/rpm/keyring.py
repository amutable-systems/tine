#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build the keyring of the signing keys a repository declares."""

import sys
from pathlib import Path
from typing import TypedDict

import specs
import util

from rpmkeys import rpmkeys


class Spec(TypedDict):
    # The declared fingerprint of each key file.
    keys: dict[str, str]
    out: str


# How rpm names an imported key in a filesystem keyring.
_KEY_PREFIX = "gpg-pubkey-"
_KEY_SUFFIX = ".key"


def keyring(spec: Spec) -> None:
    """Import the declared key files into a keyring, refusing any set but the declared fingerprints."""
    out = Path(spec["out"])
    out.mkdir(parents=True)
    if rpmkeys(out, "--import", *spec["keys"].values()):
        util.fail("keyring: importing the declared keys failed (rpmkeys output above)")

    # rpm names each imported key by fingerprint, which checks the declaration without an OpenPGP parser.
    imported = {
        path.name.removeprefix(_KEY_PREFIX).removesuffix(_KEY_SUFFIX).upper() for path in out.iterdir()
    }
    declared = {fingerprint.upper() for fingerprint in spec["keys"]}
    if imported != declared:
        absent = sorted(declared - imported)
        undeclared = sorted(imported - declared)
        util.fail(
            f"keyring: the key files are not the declared keys: absent {absent}, undeclared {undeclared}"
        )
    print(f"keyring: holds {len(imported)} declared key(s)", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    keyring(specs.parse(Spec, "keyring", argv))


if __name__ == "__main__":
    main()
