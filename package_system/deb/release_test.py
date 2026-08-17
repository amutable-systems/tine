# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for reading a Release."""

import unittest

import release


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


class TestNamed(unittest.TestCase):
    def test_accepts_the_suite_by_name_or_codename(self) -> None:
        stanza = {"suite": "testing", "codename": "forky"}
        release.named("test", stanza, "testing")
        release.named("test", stanza, "forky")

    def test_refuses_the_release_of_another_suite(self) -> None:
        for stanza in ({"suite": "oldstable", "codename": "bookworm"}, {}):
            with self.subTest(stanza=stanza), self.assertRaises(SystemExit):
                release.named("test", stanza, "testing")


if __name__ == "__main__":
    unittest.main()
