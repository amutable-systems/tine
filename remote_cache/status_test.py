# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The socket a running shim answers on, and what it says when nothing is."""

import tempfile
import unittest
from pathlib import Path
from typing import Any, override

import status


class TestStatus(unittest.TestCase):
    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.said: dict[str, Any] = {"pid": 1, "blobs": 0}

    def serving(self) -> None:
        self.enterContext(status.serving(self.root, lambda: self.said))

    def test_it_answers_with_what_the_report_says(self) -> None:
        self.serving()
        self.assertEqual(status.ask(self.root), self.said)

    def test_a_client_registers_and_is_answered_in_the_same_call(self) -> None:
        """A build says it is starting where it asks whether this is the shim it meant."""
        registered: list[int] = []
        self.enterContext(status.serving(self.root, lambda: self.said, registered.append))
        self.assertEqual(status.ask(self.root, client=4242), self.said)
        self.assertEqual(registered, [4242])

    def test_the_answer_is_made_fresh_each_time(self) -> None:
        """Counts move while a build runs, and a status that lags is worse than none."""
        self.serving()
        self.said["blobs"] = 42
        self.assertEqual((status.ask(self.root) or {})["blobs"], 42)

    def test_nothing_serving_is_an_answer_not_an_error(self) -> None:
        """Whether there is no socket, or a killed shim left one behind with nothing listening."""
        self.assertIsNone(status.ask(self.root))
        status.socket_path(self.root).touch()
        self.assertIsNone(status.ask(self.root))

    def test_the_socket_path_is_short_and_its_own_however_deep_the_store_is(self) -> None:
        """AF_UNIX has about a hundred bytes to spend, and a real store sits well down a tree."""
        deep = self.root / ("nested/" * 30)
        self.assertLess(len(str(status.socket_path(deep))), 100)
        self.assertNotEqual(status.socket_path(deep), status.socket_path(self.root))

    def test_a_leftover_socket_does_not_stop_the_next_shim(self) -> None:
        status.socket_path(self.root).touch()
        self.serving()
        self.assertEqual(status.ask(self.root), self.said)

    def test_closing_takes_the_socket_away(self) -> None:
        with status.serving(self.root, lambda: self.said) as path:
            self.assertTrue(path.exists())
        self.assertFalse(path.exists())
