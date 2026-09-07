#!/usr/bin/python3
"""Verify one upstream rpm's signature against a repository's keyring."""

from pathlib import Path
from typing import TypedDict

import specs
import util

from rpmkeys import rpmkeys


class Spec(TypedDict):
    keyring: str
    # The pool names the package file by checksum; this is the name the repository serves it under.
    name: str
    package: str
    out: str


def verify(spec: Spec) -> None:
    """Require a valid signature from the keyring on one rpm, then publish it as its verified copy."""
    package = Path(spec["package"])
    # Upstream rpm's default verify level `digest` accepts an unsigned package and Fedora's `all` does
    # not; pinning `signature` keeps the check independent of the box's rpm configuration.
    check = ("--define", "_pkgverify_level signature", "--checksig", "-v", str(package))
    if rpmkeys(Path(spec["keyring"]), *check):
        util.fail(f"verify: {spec['name']} has no valid signature from the declared keys, see above")
    util.clone_file(package, Path(spec["out"]))


def main(argv: list[str] | None = None) -> None:
    verify(specs.parse(Spec, "verify", argv))


if __name__ == "__main__":
    main()
