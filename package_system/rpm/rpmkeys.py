# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Run rpmkeys against a filesystem keyring."""

import subprocess
import tempfile
from pathlib import Path


def rpmkeys(keyring: Path, *args: str) -> int:
    """Run rpmkeys against a keyring directory, returning its exit status."""
    # rpm 6's filesystem keyring backend, rather than gpg-pubkey packages in an rpmdb, so no key
    # material ends up in any assembled root. rpm still opens a database, give it an empty scratch one.
    # Both paths are joined onto the root rpm operates on, not the working directory, hence absolute.
    with tempfile.TemporaryDirectory() as scratch:
        command = [
            "rpmkeys",
            "--define",
            "_keyring fs",
            "--define",
            f"_keyringpath {keyring.absolute()}",
            "--dbpath",
            scratch,
            *args,
        ]
        return subprocess.run(command).returncode
