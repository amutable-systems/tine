# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The bucket layer and the pointer framing, without a server or a build."""

import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from typing import override

import bucket
import mock_bucket


class TestReader(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.served = mock_bucket.Served(Path(cls.directory.name))
        cls.served.broken.add("bundle/broken")
        cls.reader = bucket.Reader(cls.served.url)

    @classmethod
    @override
    def tearDownClass(cls) -> None:
        cls.served.close()
        cls.directory.cleanup()

    def test_a_stored_object_reads_back(self) -> None:
        self.served.put("ac/" + "a" * 64, b"some bytes")
        self.assertEqual(self.reader.get("ac/" + "a" * 64), b"some bytes")
        # Saying who we are is what gets a public R2 bucket to answer at all.
        self.assertEqual(self.served.agents, {bucket.USER_AGENT})

    def test_a_missing_object_is_none(self) -> None:
        self.assertIsNone(self.reader.get("ac/" + "b" * 64))

    def test_a_broken_endpoint_is_not_a_missing_object(self) -> None:
        """The distinction the whole read path rests on: 503 must never read as "not cached"."""
        with self.assertRaises(urllib.error.HTTPError):
            self.reader.get("bundle/broken")
        # And an endpoint that answered, however badly, is asked again.
        self.assertIsNone(self.reader.get("ac/" + "b" * 64))

    def test_an_object_over_the_limit_is_refused_before_it_is_read(self) -> None:
        """The operator chooses the object's size, and every other check runs after the download."""
        self.served.put("bundle/" + "f" * 64, b"x" * 100)
        self.assertEqual(len(self.reader.get("bundle/" + "f" * 64, 100) or b""), 100)
        with self.assertRaisesRegex(ValueError, "over 99 bytes"):
            self.reader.get("bundle/" + "f" * 64, 99)

    def test_an_unreachable_endpoint_is_left_alone_for_a_while(self) -> None:
        """A build has hundreds of lookups, and each must not wait out a timeout on a dead network."""
        reader = bucket.Reader("http://127.0.0.1:1", cooldown=0.2)
        with self.assertRaises(urllib.error.URLError):
            reader.get("ac/" + "c" * 64)
        reader.base_url = self.served.url
        with self.assertRaisesRegex(urllib.error.URLError, "not asked again"):
            reader.get("ac/" + "c" * 64)
        time.sleep(0.2)
        self.assertIsNone(reader.get("ac/" + "c" * 64))
