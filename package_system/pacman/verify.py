#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Verify upstream alpm packages' signatures against a repository's keyring."""

import base64
import binascii
import sys
import tempfile
from pathlib import Path
from typing import TypedDict

import specs
import util

import alpm
import snapshotter
from gnupg import gpg


class Spec(TypedDict):
    keyring: str
    out: str
    # name of verified copy in `out` (<package name>--<sha256>.pkg.tar.zst) → original unverified
    # <sha256>.pkg.tar.zst
    packages: dict[str, str]
    # The pinned repository directory: its database carries each package's detached signature,
    # and the databases committed locks retain those of packages the pinned one dropped.
    repository: str


# What gpg must say for a signature to count: good, and by a key the keyring's trust model
# validates. Either trust line is a valid key; every other outcome is a rejection.
_TRUSTED = ("TRUST_FULLY", "TRUST_ULTIMATE")


def signatures(generation: Path) -> dict[str, bytes]:
    """The detached signature of each package one generation's database describes, by checksum."""
    databases = sorted(generation.glob("*.db"))
    if len(databases) != 1:
        util.fail(f"verify: expected exactly one *.db in {generation}, found {len(databases)}")
    found = {}
    for package in alpm.read_db(databases[0], databases[0].stem):
        if not package.signature:
            continue
        try:
            found[package.sha256] = base64.b64decode(package.signature, validate=True)
        except binascii.Error:
            util.fail(f"verify: {databases[0].name}: {package.id} has an undecodable %PGPSIG%")
    return found


class Signatures:
    """The signatures the repository carries: the pinned database's, then what locks retained.

    A retained database is read only once a package the pinned one does not describe asks for it,
    so what a lock retained is a concern of the packages that need it and of no other. It is judged
    by the same keyring, whose clock is stopped at the repository's pin: validity is computed once,
    when the keyring is built, so a packager key that expired between a lock's pin and the
    repository's is refused until the lock is refreshed.
    """

    def __init__(self, repository: Path) -> None:
        pinned, *self._retained = snapshotter.generations(repository)
        self._known = signatures(pinned)

    def get(self, checksum: str) -> bytes | None:
        while checksum not in self._known and self._retained:
            for digest, signature in signatures(self._retained.pop(0)).items():
                self._known.setdefault(digest, signature)
        return self._known.get(checksum)


def _checksum(package: str) -> str:
    """The checksum a pool artifact is named by."""
    checksum, stem, _ = Path(package).name.partition(alpm.PACKAGE_STEM)
    if not stem or len(checksum) != 64:
        util.fail(f"verify: {package} is not named by its checksum")
    return checksum.lower()


def _check(home: Path, signature: Path, package: str) -> str | None:
    """Why gpg rejects the signature over `package`, or None when it is good and from a valid key."""
    result = gpg(home, "--status-fd", "1", "--verify", str(signature), package, capture=True)
    words = [line.split()[1] for line in result.stdout.splitlines() if line.startswith("[GNUPG:] ")]
    # gpg reports each signature in a file in turn, so the lines of two would have to be told apart
    # by position; a package carries one signature, and demanding that keeps the reading flat.
    if words.count("NEWSIG") != 1:
        return f"carries {words.count('NEWSIG')} signatures, expected one"
    status = set(words)
    if result.returncode == 0 and "GOODSIG" in status and status & set(_TRUSTED):
        return None
    if result.returncode == 0 and "GOODSIG" in status:
        trust = sorted(word for word in status if word.startswith("TRUST_"))
        return f"signed by a key the declared main keys do not vouch for ({', '.join(trust)})"
    outcome = sorted(status - {"NEWSIG", "KEY_CONSIDERED"})
    return f"{', '.join(outcome) or 'gpg failed'}: {result.stderr.strip()}"


def verify(spec: Spec) -> None:
    """Require a valid signature from a vouched-for key on every package, then publish verified copies."""
    out = Path(spec["out"])
    out.mkdir(parents=True)
    known = Signatures(Path(spec["repository"]))
    home = Path(spec["keyring"])
    rejected = []
    with tempfile.TemporaryDirectory(prefix="verify.") as scratch:
        signature = Path(scratch) / "package.sig"
        for name, package in spec["packages"].items():
            detached = known.get(_checksum(package))
            if detached is None:
                rejected.append(
                    f"{name}: no database the repository carries has a signature for it; a lock that"
                    " selected it before its metadata was retained needs re-resolving"
                )
                continue
            signature.write_bytes(detached)
            reason = _check(home, signature, package)
            if reason is not None:
                rejected.append(f"{name}: {reason}")
    if rejected:
        util.fail("verify: no valid signature from the declared keys on\n  " + "\n  ".join(rejected))
    for name, package in spec["packages"].items():
        util.clone_file(Path(package), out / name)
    print(f"verify: {len(spec['packages'])} package(s) verified", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    verify(specs.parse(Spec, "verify", argv))


if __name__ == "__main__":
    main()
