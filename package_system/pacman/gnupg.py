# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Run gpg against a home directory this package system built."""

import subprocess
from pathlib import Path

# pacman-key's own settings, plus the trust model stated rather than left to gpg's defaults: a key
# with marginal ownertrust is one main key, and three of them make a packager's key valid. Validity
# is computed once, when the keyring is built; a verification never rewrites the trustdb, so the
# keyring can be read-only and shared.
GPG_CONF = """\
no-greeting
no-permission-warning
lock-never
no-auto-check-trustdb
trust-model pgp
marginals-needed 3
"""

# What a built keyring consists of: the keys, the validity computed over them, and the settings.
# The throwaway certification key's secret half and gpg's backups stay behind in scratch.
KEYRING_FILES = ("gpg.conf", "pubring.kbx", "trustdb.gpg")


def gpg(
    home: Path,
    *args: str,
    stdin: str | None = None,
    capture: bool = False,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run gpg with the given home directory, never asking anything."""
    return subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", "--no-tty", *args],
        input=stdin,
        capture_output=capture,
        check=check,
        text=True,
    )


def fingerprints(home: Path, *args: str) -> list[str]:
    """The primary fingerprints of the keys one listing command shows, in listing order."""
    return primary_fingerprints(gpg(home, "--with-colons", *args, capture=True, check=True).stdout)


def primary_fingerprints(listing: str) -> list[str]:
    """The primary fingerprints in a `--with-colons` listing, in listing order."""
    primary = []
    after_key = False
    for line in listing.splitlines():
        fields = line.split(":")
        if fields[0] in ("pub", "sec"):
            after_key = True
        elif fields[0] == "fpr" and after_key:
            # The first fingerprint after a key is the primary's; a subkey's follows its own `sub`.
            primary.append(fields[9].upper())
            after_key = False
    return primary


def kill_agent(home: Path) -> None:
    """Stop the agent gpg started for this home directory."""
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], check=True)
