# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for template expansion.

buck test tine//image:test
"""

import unittest

import substitute

UNIT = """\
[Service]
ExecStart=@bindir@/quarry-client http -H 127.0.0.1:@port@

[Socket]
ListenStream=127.0.0.1:@port@
"""


class TestExpand(unittest.TestCase):
    def test_expands_every_occurrence(self) -> None:
        self.assertEqual(
            substitute.expand("units", UNIT, {"@bindir@": "/usr/bin", "@port@": "555"}),
            "[Service]\n"
            "ExecStart=/usr/bin/quarry-client http -H 127.0.0.1:555\n"
            "\n"
            "[Socket]\n"
            "ListenStream=127.0.0.1:555\n",
        )

    def test_leaves_a_marker_it_was_not_given_alone(self) -> None:
        """Only what is declared is expanded; the rest is the template's own text."""
        self.assertIn("@port@", substitute.expand("units", UNIT, {"@bindir@": "/usr/bin"}))

    def test_rejects_a_placeholder_the_template_does_not_hold(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            substitute.expand("units", UNIT, {"@sysconfdir@": "/etc"})
        self.assertEqual(
            str(caught.exception),
            "tine: substitute units: the template holds no @sysconfdir@",
        )
