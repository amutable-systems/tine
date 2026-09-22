# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The bundle container, on its own: no protobuf, no server, no bucket."""

import hashlib
import unittest

import bundle
from reapi import Digest


def blobs(*bodies: bytes) -> dict[str, bytes]:
    return {hashlib.sha256(body).hexdigest(): body for body in bodies}


def member(name: str, size: int, data: bytes) -> bytes:
    """A member as written, with a header that need not tell the truth about the bytes."""
    return bundle.HEADER.pack(name.encode(), size) + data


class TestBundleName(unittest.TestCase):
    def test_the_name_covers_the_member_set(self) -> None:
        one = bundle.bundle_name([Digest("a" * 64, 1)])
        other = bundle.bundle_name([Digest("b" * 64, 1)])
        self.assertNotEqual(one, other)

    def test_the_name_ignores_order_and_repetition(self) -> None:
        members = [Digest("a" * 64, 1), Digest("b" * 64, 2)]
        self.assertEqual(bundle.bundle_name(members), bundle.bundle_name(reversed(members)))
        self.assertEqual(bundle.bundle_name(members), bundle.bundle_name(members + members))

    def test_the_name_covers_the_size(self) -> None:
        """Two blobs cannot share a hash, but a wrong size must still name a different bundle."""
        self.assertNotEqual(
            bundle.bundle_name([Digest("a" * 64, 1)]), bundle.bundle_name([Digest("a" * 64, 2)])
        )


class TestPackAndUnpack(unittest.TestCase):
    def test_a_round_trip_returns_every_member(self) -> None:
        original = blobs(b"one", b"two", b"", b"x" * 100000)
        self.assertEqual(dict(bundle.unpack(bundle.pack(original))), original)
        self.assertEqual(list(bundle.unpack(bundle.pack({}))), [])

    def test_packing_is_deterministic(self) -> None:
        original = blobs(b"one", b"two")
        self.assertEqual(bundle.pack(original), bundle.pack(dict(reversed(original.items()))))

    def test_a_member_that_does_not_hash_to_its_name_is_refused(self) -> None:
        """The bucket is writable by someone else, so a bundle's own naming has to be checked."""
        with self.assertRaisesRegex(ValueError, "hashes to"):
            list(bundle.unpack(member("a" * 64, 3, b"lie")))

    def test_bytes_that_are_not_a_bundle_are_refused_the_same_way(self) -> None:
        """A rewritten object in the bucket is a decision about the bucket, not a traceback."""
        with self.assertRaisesRegex(ValueError, "not a member"):
            list(bundle.unpack(b"not a bundle"))
        with self.assertRaisesRegex(ValueError, "not a member"):
            list(bundle.unpack(bundle.pack(blobs(b"one")) + b"\0"))

    def test_a_member_claiming_more_than_the_bundle_holds_is_refused(self) -> None:
        """A size is read from the header, so it is checked against the bytes before it is believed."""
        for size in (4, 256 * 1024**2, 2**64 - 1):
            with self.subTest(size=size), self.assertRaisesRegex(ValueError, "claims"):
                list(bundle.unpack(member("a" * 64, size, b"lie")))
