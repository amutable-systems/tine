# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for UAPI version comparison.

buck test tine//image:test
"""

import unittest

import version


class CompareTest(unittest.TestCase):
    def assert_equal(self, left: str, right: str) -> None:
        with self.subTest(left=left, right=right):
            self.assertEqual(version.compare(left, right), 0)
            self.assertEqual(version.compare(right, left), 0)

    def assert_older(self, older: str, newer: str) -> None:
        with self.subTest(older=older, newer=newer):
            self.assertEqual(version.compare(older, newer), -1)
            self.assertEqual(version.compare(newer, older), 1)

    def test_reference_order(self) -> None:
        ordered = (
            "122.1",
            "123~rc1-1",
            "123",
            "123-a",
            "123-a.1",
            "123-1",
            "123-1.1",
            "123^post1",
            "123.a-1",
            "123.1-1",
            "123a-1",
            "124-1",
        )
        for index, older in enumerate(ordered):
            for newer in ordered[index + 1 :]:
                self.assert_older(older, newer)

    def test_ignored_separators_and_non_ascii_characters(self) -> None:
        for left, right in (
            ("11α", "11β"),
            ("1_", "1"),
            ("_1", "1"),
            ("1+", "1"),
            ("+1", "1"),
        ):
            self.assert_equal(left, right)
        self.assert_older("1_", "1.2")
        self.assert_older("1.3.3", "1_2_3")
        self.assert_older("1+", "1.2")
        self.assert_older("1.3.3", "1+2+3")

    def test_empty_and_tilde(self) -> None:
        self.assert_older("~", "")
        self.assert_older("~", "0")
        self.assert_older("", "0")

    def test_marker_only_strings(self) -> None:
        for value in ("-", "^", ".", "--", "^^", ".."):
            self.assert_equal(value, value)

    def test_letters_use_ascii_order(self) -> None:
        self.assert_older("bar-123", "foo-123")
        self.assert_older("B", "a")
        self.assert_older("123.a", "123.b")
        self.assert_older("123.a", "123a")

    def test_numeric_components_ignore_leading_zeroes(self) -> None:
        self.assert_equal("10.0001", "10.1")

    def test_numeric_components_sort_after_alphabetic_components(self) -> None:
        self.assert_older("a", "0")

    def test_numeric_components_have_no_integer_size_limit(self) -> None:
        self.assert_older("9" * 5000, "1" + "0" * 5000)
