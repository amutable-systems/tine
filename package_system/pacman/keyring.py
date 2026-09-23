#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build the keyring that verifies an alpm repository's packages.

Arch signs each package with an individual packager's key and vouches for those keys through
certifications by its main keys, all shipped together in `archlinux-keyring`. The declared signing
keys are the main keys. The box's copy of that package supplies the packagers' keys and the
certifications, and a packager's key counts once three declared main keys certify it: pacman-key's
trust model, with the catalog rather than the package deciding which main keys count.
"""

import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import specs
import util

from gnupg import GPG_CONF, KEYRING_FILES, fingerprints, gpg, kill_agent, primary_fingerprints


class Spec(TypedDict):
    # The declared fingerprint of each key file.
    keys: dict[str, str]
    out: str
    # When the repository's snapshot was published, ISO 8601 in UTC, or None to judge as of now.
    time: str | None


# Where the keyring package installs its build, which `pacman-key --populate` reads from.
KEYRINGS = Path("/usr/share/pacman/keyrings")
KEYRING = "archlinux"

# gpg's marginal ownertrust, what archlinux-trusted assigns each main key.
_MARGINAL = 4


def _fingerprint_list(path: Path) -> set[str]:
    return {line.strip().upper() for line in path.read_text().splitlines() if line.strip()}


def _configuration(time: str | None) -> str:
    """gpg.conf for the keyring, with its clock stopped at the snapshot where there is one.

    A pin freezes the trust data, so the clock goes with it: keys and certifications are judged as
    of the snapshot, and a pinned repository verifies the same way however long after it is built.
    """
    if time is None:
        return GPG_CONF
    when = datetime.fromisoformat(time)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    # Frozen with the "!": without it each gpg process starts at the given time and runs on from
    # there, so a key made a second later than the process that next reads it is "from the future".
    return GPG_CONF + f"faked-system-time {when.astimezone(UTC).strftime('%Y%m%dT%H%M%S')}!\n"


def _populate(home: Path, spec: Spec, keyrings: Path) -> None:
    declared = {fingerprint.upper() for fingerprint in spec["keys"]}
    package = keyrings / f"{KEYRING}.gpg"
    if not package.is_file():
        util.fail(f"keyring: {package} is missing; the box carries no {KEYRING}-keyring")
    (home / "gpg.conf").write_text(_configuration(spec["time"]))

    # A certification counts only from a valid key, and validity comes from a key trusted
    # ultimately, which is one with a secret half here. This throwaway key certifies the declared
    # main keys locally, as `pacman-key --populate` does, and their marginal ownertrust does the rest.
    generate = ("--passphrase", "", "--pinentry-mode", "loopback", "--quick-generate-key")
    if gpg(home, "--quiet", *generate, "tine keyring", "ed25519", "cert", "never").returncode:
        util.fail("keyring: generating the local certification key failed (gpg output above)")
    (local,) = fingerprints(home, "--list-secret-keys")

    if gpg(home, "--quiet", "--import", str(package)).returncode:
        util.fail(f"keyring: importing {package} failed (gpg output above)")

    # Each declared file must hold the key it is declared as and nothing else; a web key directory
    # serves the same certificate twice, which is still nothing else.
    for fingerprint, file in spec["keys"].items():
        shown = gpg(home, "--with-colons", "--show-keys", file, capture=True)
        if shown.returncode:
            util.fail(f"keyring: {file} is not a key file: {shown.stderr.strip()}")
        found = sorted(set(primary_fingerprints(shown.stdout)))
        if found != [fingerprint.upper()]:
            util.fail(f"keyring: {file} holds {found}, not the declared key {fingerprint}")
    if gpg(home, "--quiet", "--import", *spec["keys"].values()).returncode:
        util.fail("keyring: importing the declared keys failed (gpg output above)")
    if fingerprints(home, "--list-secret-keys") != [local]:
        util.fail("keyring: a declared key file carries a secret key, which no public key file does")

    # The keyring package withdraws keys its maintainers revoked on the owner's behalf, which no
    # revocation certificate in the keyring says.
    revoked = _fingerprint_list(keyrings / f"{KEYRING}-revoked")
    withdrawn = sorted(declared & revoked)
    if withdrawn:
        util.fail(f"keyring: declared key(s) {withdrawn} are revoked by {package.name}")
    present = set(fingerprints(home, "--list-keys"))
    drop = sorted(present & revoked)
    if drop and gpg(home, "--quiet", "--yes", "--delete-keys", *drop).returncode:
        util.fail("keyring: dropping the revoked keys failed (gpg output above)")

    for fingerprint in sorted(declared):
        # Captured: gpg narrates every certification it makes, whichever quiet option it is given.
        certify = gpg(home, "--yes", "--local-user", local, "--quick-lsign-key", fingerprint, capture=True)
        if certify.returncode:
            util.fail(f"keyring: certifying the declared key {fingerprint} failed: {certify.stderr.strip()}")
    ownertrust = "".join(f"{fingerprint}:{_MARGINAL}:\n" for fingerprint in sorted(declared))
    if gpg(home, "--quiet", "--import-ownertrust", stdin=ownertrust).returncode:
        util.fail("keyring: setting the declared keys' ownertrust failed (gpg output above)")
    check = gpg(home, "--check-trustdb", capture=True)
    if check.returncode:
        util.fail(f"keyring: computing key validity failed: {check.stderr.strip()}")

    # gpg reports success for a certification it could not make, such as of a key without a user
    # ID to certify, so what matters is checked directly: every declared key came out valid.
    for fingerprint in sorted(declared):
        listing = gpg(home, "--with-colons", "--list-keys", fingerprint, capture=True, check=True).stdout
        validity = next(line.split(":")[1] for line in listing.splitlines() if line.startswith("pub:"))
        if validity not in ("f", "u"):
            util.fail(
                f"keyring: the declared key {fingerprint} is not valid after certification ({validity!r})"
            )
    print(
        f"keyring: holds {len(present - revoked)} keys, {len(declared)} declared main key(s)",
        file=sys.stderr,
    )


def keyring(spec: Spec, keyrings: Path = KEYRINGS) -> None:
    """Build a gpg home directory in which the declared main keys vouch for the repository's packagers."""
    out = Path(spec["out"])
    # The agent's socket lives in the home directory, and a socket path is limited to a length a
    # Buck output path exceeds, so the keyring is built under scratch and what it consists of is
    # copied into place.
    with tempfile.TemporaryDirectory(prefix="keyring.") as scratch:
        home = Path(scratch) / "gnupg"
        home.mkdir(mode=0o700)
        try:
            _populate(home, spec, keyrings)
        finally:
            kill_agent(home)
        out.mkdir(parents=True, mode=0o700)
        for name in KEYRING_FILES:
            shutil.copyfile(home / name, out / name)


def main(argv: list[str] | None = None) -> None:
    keyring(specs.parse(Spec, "keyring", argv))


if __name__ == "__main__":
    main()
