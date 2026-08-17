#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Verify upstream debs against a repository's signed Release.

Debian signs the Release and nothing below it: the Release states the checksum of every index,
and the index that of every package. So the chain is followed from a pinned Release, whose
signature sqv checks against the keyring, down to each selected package's bytes. A committed lock
retains the Release it was resolved against, so a package the current one no longer describes is
vouched for by that retained generation.
"""

import hashlib
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TypedDict

import deb822
import openpgp
import release
import specs
import util

import snapshotter


class Spec(TypedDict):
    # What the repository was declared as: the Release has to state the pinned index under the
    # directory these two name, so a snapshot of the wrong component or architecture is refused.
    arch: str
    component: str
    keyring: str
    out: str
    # name of verified copy in `out` (<deb name>--<sha256>.deb) → original unverified <sha256>.deb
    packages: dict[str, str]
    # The pinned repository directory: the signed Release and the one index it vouches for, and
    # the generations of both that committed locks retain.
    repository: str


def authenticated(keyring: Path, signed: Path, message: Path, time: str | None) -> bytes:
    """The Release's message, once a declared key's signature over it holds as of `time`.

    One good signature from a declared key is what counts, as it is for APT: the archive signs
    with its current key and the one before, and a keyring declaring either verifies.
    """
    if not signed.is_file():
        util.fail(f"verify: {signed.parent} carries no {signed.name}; run refresh-catalog")
    command = ["sqv", "--keyring", str(keyring / openpgp.KEYRING_FILE)]
    if time is not None:
        command += ["--time", time]
    command += ["--cleartext", "--output", str(message), str(signed)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        util.fail(
            f"verify: {signed.name}: no valid signature from the declared keys: {result.stderr.strip()}"
        )
    print(f"verify: {signed.name} signed by {' '.join(result.stdout.split())}", file=sys.stderr)
    return message.read_bytes()


def _digest(path: Path) -> tuple[str, int]:
    with path.open("rb") as raw:
        return hashlib.file_digest(raw, "sha256").hexdigest(), path.stat().st_size


def vouched(stated: dict[str, tuple[str, int]], index: Path, where: str) -> None:
    """Require the pinned index to be the one the Release states at `where`, byte for byte.

    Matched by the path the declaration asks for rather than by name alone: a suite states an index
    per component and architecture, and every one of them is signed, so a name is not an identity.
    """
    path = f"{where}/{index.name}"
    digest, size = _digest(index)
    if stated.get(path) != (digest, size):
        util.fail(f"verify: the signed Release states no {path} of checksum {digest} and {size} bytes")


def described(index: Path) -> dict[str, int]:
    """The size of every package the index describes, by checksum."""
    packages: dict[str, int] = {}
    with deb822.open_text(index) as source:
        for position, stanza in enumerate(deb822.stanzas(source), 1):
            where = f"{index.name} record {position}"
            digest = deb822.required(stanza, "sha256", where).lower()
            packages[digest] = deb822.integer(
                deb822.required(stanza, "size", where), f"{where} Size", minimum=1
            )
    return packages


def _stated_at(what: str, value: str) -> datetime:
    """A date a Release states about itself, as RFC 2822 writes one."""
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError:
        util.fail(f"verify: {what} is not an RFC 2822 date: {value!r}")
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def current(what: str, stanza: dict[str, str], time: str | None) -> None:
    """Require the Release to have been the current one at the time it is judged as of.

    A signature only says when it was made, so a genuine Release the archive has long since
    superseded keeps verifying against the key that signed it. Debian states the window it vouches
    for itself, so a pin is held to that window: a mirror cannot answer with an older tree than the
    one the pin names. An unpinned repository is judged as of the build, which is what APT does.
    """
    at = datetime.now(UTC) if time is None else datetime.fromisoformat(time)
    signed = _stated_at(f"{what} Date", deb822.required(stanza, "date", what))
    if signed > at:
        util.fail(
            f"verify: {what} is dated {signed.isoformat()}, after the {at.isoformat()} it is judged as of"
        )
    until = stanza.get("valid-until")
    if until is not None and _stated_at(f"{what} Valid-Until", until) < at:
        util.fail(f"verify: {what} expired at {until}, before the {at.isoformat()} it is judged as of")


def vouched_packages(keyring: Path, generation: Path, scratch: Path, where: str) -> dict[str, int]:
    """The size of every package one generation's signed Release vouches for, by checksum.

    A generation is judged as of its own pin: the repository's for the pinned one, which the
    keyring carries, and the one a lock recorded for a generation it retains.
    """
    time = snapshotter.pinned_at(generation)
    if time is None and (keyring / openpgp.TIME_FILE).is_file():
        time = (keyring / openpgp.TIME_FILE).read_text().strip()
    message = authenticated(
        keyring, generation / release.INRELEASE, scratch / f"{generation.name}.Release", time
    )
    stanza = release.stanza(release.INRELEASE, message)
    current(release.INRELEASE, stanza, time)
    index = deb822.package_index(generation, release.INRELEASE)
    vouched(release.stated(release.INRELEASE, stanza), index, where)
    return described(index)


class Vouched:
    """What the repository's signed Releases vouch for: the pinned one's index, then what locks retained.

    A retained generation is authenticated only once a package the pinned index does not describe
    asks for it, so what a lock retained is a concern of the packages that need it and of no other.
    """

    def __init__(self, keyring: Path, repository: Path, scratch: Path, where: str) -> None:
        self._keyring = keyring
        self._scratch = scratch
        self._where = where
        pinned, *self._retained = snapshotter.generations(repository)
        self._known = vouched_packages(keyring, pinned, scratch, where)

    def size(self, digest: str) -> int | None:
        """The size the first vouching index states for the package of this checksum."""
        while digest not in self._known and self._retained:
            generation = vouched_packages(self._keyring, self._retained.pop(0), self._scratch, self._where)
            for checksum, size in generation.items():
                self._known.setdefault(checksum, size)
        return self._known.get(digest)


def _checksum(package: str) -> str:
    """The checksum a pool artifact is named by."""
    name = Path(package).name
    checksum = name.removesuffix(".deb")
    if checksum == name or len(checksum) != 64:
        util.fail(f"verify: {package} is not named by its checksum")
    return checksum.lower()


def verify(spec: Spec) -> None:
    """Require every package to be one the signed Release vouches for, then publish verified copies."""
    out = Path(spec["out"])
    out.mkdir(parents=True)
    keyring = Path(spec["keyring"])
    rejected = []
    with tempfile.TemporaryDirectory(prefix="verify.") as scratch:
        where = f"{spec['component']}/binary-{spec['arch']}"
        vouched = Vouched(keyring, Path(spec["repository"]), Path(scratch), where)
        for name, package in spec["packages"].items():
            digest, size = _digest(Path(package))
            if digest != _checksum(package):
                rejected.append(f"{name}: its contents are not the checksum it is named by")
                continue
            stated = vouched.size(digest)
            if stated is None:
                rejected.append(
                    f"{name}: no signed Release the repository carries vouches for checksum {digest}; a"
                    " lock that selected it before its metadata was retained needs re-resolving"
                )
            elif stated != size:
                rejected.append(f"{name}: is {size} bytes, not the {stated} the vouched-for index states")
    if rejected:
        util.fail("verify: not vouched for by the signed Release\n  " + "\n  ".join(rejected))
    for name, package in spec["packages"].items():
        util.clone_file(Path(package), out / name)
    print(f"verify: {len(spec['packages'])} package(s) verified", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    verify(specs.parse(Spec, "verify", argv))


if __name__ == "__main__":
    main()
