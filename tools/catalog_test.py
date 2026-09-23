# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the catalog tool's host-side key handling."""

import base64
import subprocess
import tempfile
import unittest
from pathlib import Path

import catalog


class TestArmor(unittest.TestCase):
    def test_matches_the_reference_check_value(self) -> None:
        # CRC-24/OPENPGP's published check value for "123456789".
        self.assertEqual(catalog._crc24(b"123456789"), 0x21CF02)

    def test_wraps_a_binary_key_the_way_gpg_reads_it(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch) / "gnupg"
            home.mkdir(mode=0o700)
            gpg = ["gpg", "--homedir", str(home), "--batch", "--quiet"]
            subprocess.run(
                [
                    *gpg,
                    "--passphrase",
                    "",
                    "--pinentry-mode",
                    "loopback",
                    "--quick-generate-key",
                    "k",
                    "ed25519",
                ],
                check=True,
            )
            binary = subprocess.run([*gpg, "--export"], check=True, stdout=subprocess.PIPE).stdout
            subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], check=True)
            armored = catalog.armor(binary)
            path = Path(scratch) / "key.asc"
            path.write_text(armored)
            shown = subprocess.run(
                [*gpg, "--show-keys", "--with-colons", str(path)],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout
            listed = subprocess.run(
                [*gpg, "--list-keys", "--with-colons"], check=True, stdout=subprocess.PIPE, text=True
            ).stdout

        def fingerprint(colons: str) -> str:
            return next(line for line in colons.splitlines() if line.startswith("fpr:"))

        self.assertEqual(fingerprint(shown), fingerprint(listed))
        self.assertTrue(armored.startswith(catalog.ARMOR_HEADER.decode() + "\n\n"))
        self.assertTrue(armored.endswith(catalog.ARMOR_FOOTER.decode() + "\n"))
        body = "".join(line for line in armored.splitlines()[2:-2])
        self.assertEqual(base64.b64decode(body), binary)


if __name__ == "__main__":
    unittest.main()
