# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the OpenPGP reader against what gpg writes."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

import openpgp


class TestOpenPGP(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls._scratch = tempfile.TemporaryDirectory()
        cls.home = Path(cls._scratch.name) / "gnupg"
        cls.home.mkdir(mode=0o700)
        cls.addClassCleanup(
            subprocess.run, ["gpgconf", "--homedir", str(cls.home), "--kill", "all"], check=True
        )
        cls.keys = {
            name: cls._generate(name, algorithm)
            for name, algorithm in (("ed", "ed25519"), ("rsa", "rsa2048"))
        }

    @classmethod
    def gpg(cls, *args: str) -> bytes:
        command = ["gpg", "--homedir", str(cls.home), "--batch", "--no-tty", "--quiet", *args]
        return subprocess.run(command, capture_output=True, check=True).stdout

    @classmethod
    def _generate(cls, name: str, algorithm: str) -> str:
        cls.gpg(
            "--passphrase",
            "",
            "--pinentry-mode",
            "loopback",
            "--quick-generate-key",
            name,
            algorithm,
            "sign",
            "never",
        )
        listing = cls.gpg("--with-colons", "--list-keys", name).decode()
        return next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")).upper()

    def armored(self, *fingerprints: str) -> str:
        return "".join(self.gpg("--export", "--armor", fingerprint).decode() for fingerprint in fingerprints)

    def test_fingerprints_are_the_ones_gpg_lists(self) -> None:
        for name, fingerprint in self.keys.items():
            with self.subTest(name=name):
                self.assertEqual(
                    openpgp.primary_fingerprints(openpgp.dearmor(self.armored(fingerprint))), [fingerprint]
                )

    def test_concatenated_blocks_read_as_one_keyring(self) -> None:
        found = openpgp.primary_fingerprints(openpgp.dearmor(self.armored(*self.keys.values())))
        self.assertEqual(found, list(self.keys.values()))

    def test_new_format_lengths_frame_the_same_packets(self) -> None:
        # gpg exports a key under the old framing; the new one has three length forms, each of
        # which must frame the same bodies.
        expected = list(openpgp.packets(self.gpg("--export", self.keys["rsa"])))
        for form in (1, 2, 5):
            with self.subTest(form=form):
                reframed = b"".join(
                    bytes([0xC0 | tag]) + _new_length(len(body), form) + body for tag, body in expected
                )
                self.assertEqual(list(openpgp.packets(reframed)), expected)

    def test_refuses_what_is_not_a_public_key_file(self) -> None:
        secret = self.gpg(
            "--passphrase",
            "",
            "--pinentry-mode",
            "loopback",
            "--export-secret-keys",
            "--armor",
            self.keys["ed"],
        )
        armored = self.armored(self.keys["ed"])
        binary = openpgp.dearmor(armored)
        for what, text in (
            ("html", "<html>Access Denied</html>\n"),
            ("secret", secret.decode()),
            ("empty block", f"{openpgp.ARMOR_HEADER}\n\n{openpgp.ARMOR_FOOTER}\n"),
            ("unterminated", armored.replace(openpgp.ARMOR_FOOTER, "")),
            ("trailing junk", armored + "junk\n"),
        ):
            with self.subTest(what=what), self.assertRaises(openpgp.Malformed):
                openpgp.primary_fingerprints(openpgp.dearmor(text))
        # The armor header alone rejects an exported secret key, so the tag check needs a stream
        # that got past it: the packets a real secret key is made of, framed as a keyring.
        secret_binary = openpgp.dearmor(
            secret.decode().replace("PGP PRIVATE KEY BLOCK", "PGP PUBLIC KEY BLOCK")
        )
        for what, data in (
            ("secret key packets", secret_binary),
            ("an oversized v4 key packet", b"\xc6" + _new_length(70000, 3) + b"\x04" + bytes(69999)),
            ("truncated", binary[:-1]),
            ("no header bit", b"\x00" + binary),
            ("partial length", b"\xc6\xe0" + binary),
            ("indeterminate length", b"\x9b" + binary),
            ("no key", bytes([0xC0 | 13, 3]) + b"uid"),
            ("foreign packet", bytes([0xC0 | 11, 3]) + b"lit" + binary),
        ):
            with self.subTest(what=what), self.assertRaises(openpgp.Malformed):
                openpgp.primary_fingerprints(data)


def _new_length(length: int, form: int) -> bytes:
    """A new-format body length in the given form, or the next one up where that cannot hold it."""
    if form == 1 and length < 192:
        return bytes([length])
    if form <= 2 and 192 <= length < 8384:
        return bytes([((length - 192) >> 8) + 192, (length - 192) & 0xFF])
    return bytes([255]) + length.to_bytes(4, "big")


if __name__ == "__main__":
    unittest.main()
