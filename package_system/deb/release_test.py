# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for reading a Release out of its clearsigned frame."""

import unittest

import release

SIGNATURE = "-----BEGIN PGP SIGNATURE-----\n\niQEzBAEBCAAdFiEE\n=abcd\n-----END PGP SIGNATURE-----\n"


def clearsigned(message: str, headers: str = "Hash: SHA256\n") -> bytes:
    return f"-----BEGIN PGP SIGNED MESSAGE-----\n{headers}\n{message}\n{SIGNATURE}".encode()


class TestUnsigned(unittest.TestCase):
    def test_undoes_the_cleartext_framing(self) -> None:
        # Dash-escaped lines, trailing whitespace and CRLF endings were never part of the message.
        message = "Origin: Debian \r\n- -- a dashed line\n- From here\nSHA256:\n abc 1 main/Packages"
        self.assertEqual(
            release.unsigned("test", clearsigned(message)),
            b"Origin: Debian\n-- a dashed line\nFrom here\nSHA256:\n abc 1 main/Packages",
        )

    def test_reads_a_message_without_hash_headers(self) -> None:
        self.assertEqual(
            release.unsigned("test", clearsigned("Suite: testing", headers="")), b"Suite: testing"
        )

    def test_refuses_what_is_not_one_clearsigned_message(self) -> None:
        for what, data in (
            ("plain", b"Suite: testing\n"),
            ("unsigned", b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nSuite: testing\n"),
            ("unterminated", clearsigned("Suite: testing").replace(b"-----END PGP SIGNATURE-----\n", b"")),
            ("trailing", clearsigned("Suite: testing") + b"Suite: unstable\n"),
        ):
            with self.subTest(what=what), self.assertRaises(SystemExit):
                release.unsigned("test", data)


class TestStanza(unittest.TestCase):
    def test_reads_the_one_stanza(self) -> None:
        self.assertEqual(
            release.stanza("test", b"Suite: testing\nCodename: forky\n"),
            {"suite": "testing", "codename": "forky"},
        )

    def test_refuses_two_stanzas_or_other_encodings(self) -> None:
        for data in (b"Suite: testing\n\nSuite: unstable\n", b"", b"Suite: \xff\n"):
            with self.subTest(data=data), self.assertRaises(SystemExit):
                release.stanza("test", data)


if __name__ == "__main__":
    unittest.main()
