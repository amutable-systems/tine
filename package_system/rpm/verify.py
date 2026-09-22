#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Verify upstream rpms' signatures against a repository's keyring."""

from pathlib import Path
from typing import TypedDict

import specs
import util

from rpmkeys import rpmkeys


class Spec(TypedDict):
    keyring: str
    out: str
    # name of verified copy in `out` (<rpm name>--<sha256>.rpm) → original unverified <sha256>.rpm
    packages: dict[str, str]


# Upstream rpm's default verify level `digest` accepts an unsigned package and Fedora's `all` does not;
# pinning `signature` keeps the check independent of the box's rpm configuration.
_CHECK = ("--define", "_pkgverify_level signature", "--checksig")


def verify(spec: Spec) -> None:
    """Require a valid signature from the keyring on every rpm, then publish them as verified copies."""
    keyring = Path(spec["keyring"])
    out = Path(spec["out"])
    out.mkdir(parents=True)
    # One rpmkeys run for the whole batch; only a failure checks the packages one by one, to name them.
    if rpmkeys(keyring, *_CHECK, "-v", *spec["packages"].values()):
        rejected = sorted(
            name for name, package in spec["packages"].items() if rpmkeys(keyring, *_CHECK, package)
        )
        if not rejected:
            util.fail("verify: rpmkeys failed, see above")
        util.fail(f"verify: no valid signature from the declared keys on {', '.join(rejected)}, see above")
    for name, package in spec["packages"].items():
        util.clone_file(Path(package), out / name)


def main(argv: list[str] | None = None) -> None:
    verify(specs.parse(Spec, "verify", argv))


if __name__ == "__main__":
    main()
