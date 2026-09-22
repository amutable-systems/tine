# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The two halves of the bucket against a real S3 implementation.

`mock_bucket.Served` covers the layout, but not the claim the layout rests on: that an object written
through the S3 API is readable, unchanged, over plain HTTP with no credentials. Verify that with SeaweedFS.
"""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from typing import override

import bucket
import bundle
import seaweed


class TestSeaweedInterop(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        # pinned binary, passed in by the test target
        weed = seaweed.Seaweed(Path(os.environ["WEED"]), Path(cls.directory.name), "tine-cache")
        cls.addClassCleanup(weed.close)
        cls.writer = bucket.S3Writer(
            endpoint=weed.s3_endpoint,
            bucket=weed.bucket,
            access_key="unchecked",
            secret_key="unchecked",
            secure=False,
        )
        cls.reader = bucket.Reader(weed.read_url)

    def test_a_pointer_written_over_s3_reads_over_http(self) -> None:
        key = "ac/" + "a" * 64
        self.writer.put(key, b"a pointer, more or less")
        self.assertEqual(self.reader.get(key), b"a pointer, more or less")

    def test_a_bundle_survives_the_round_trip(self) -> None:
        blobs = {hashlib.sha256(body).hexdigest(): body for body in (b"one", b"two", b"x" * 70000)}
        key = "bundle/" + "b" * 64
        self.writer.put(key, bundle.pack(blobs))
        fetched = self.reader.get(key)
        assert fetched is not None
        self.assertEqual(dict(bundle.unpack(fetched)), blobs)

    def test_a_missing_key_is_none_and_not_an_error(self) -> None:
        self.assertIsNone(self.reader.get("ac/" + "c" * 64))

    def test_a_refresh_tells_present_from_absent_and_keeps_the_bytes(self) -> None:
        key = "bundle/" + "e" * 64
        self.assertFalse(self.writer.refresh(key))
        self.writer.put(key, b"shared")
        self.assertTrue(self.writer.refresh(key))
        self.assertEqual(self.reader.get(key), b"shared")
