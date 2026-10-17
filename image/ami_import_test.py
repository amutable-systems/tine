# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for picking the blocks of an AMI disk to upload.

buck test tine//image:test
"""

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

import ami_import
from ami_import import BLOCK_SIZE


class DataBlocksTest(unittest.TestCase):
    def disk(self, size: int, writes: dict[int, bytes]) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="ami-import-test.")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "disk.raw"
        with path.open("wb") as f:
            f.truncate(size)
            for offset, data in writes.items():
                f.seek(offset)
                f.write(data)
        return path

    def test_yields_only_blocks_with_data(self) -> None:
        disk = self.disk(
            8 * BLOCK_SIZE,
            {
                # allocated, but zero: nothing to upload
                0: bytes(BLOCK_SIZE),
                # stretches across blocks 2 and 3
                3 * BLOCK_SIZE - 1: b"ab",
                7 * BLOCK_SIZE + 5: b"c",
            },
        )
        self.assertEqual([index for index, _ in ami_import.data_blocks(disk)], [2, 3, 7])

    def test_blocks_are_whole_and_in_place(self) -> None:
        disk = self.disk(4 * BLOCK_SIZE, {BLOCK_SIZE + 3: b"x"})
        [(index, block)] = ami_import.data_blocks(disk)
        self.assertEqual(index, 1)
        self.assertEqual(block, bytes(3) + b"x" + bytes(BLOCK_SIZE - 4))

    def test_pads_a_short_last_block(self) -> None:
        disk = self.disk(BLOCK_SIZE + 10, {BLOCK_SIZE: b"y"})
        [(index, block)] = ami_import.data_blocks(disk)
        self.assertEqual(index, 1)
        self.assertEqual(block, b"y" + bytes(BLOCK_SIZE - 1))

    def test_empty_disk_has_no_blocks(self) -> None:
        self.assertEqual(list(ami_import.data_blocks(self.disk(3 * BLOCK_SIZE, {}))), [])


class ChecksumTest(unittest.TestCase):
    def test_is_base64_sha256(self) -> None:
        block = b"z" * BLOCK_SIZE
        self.assertEqual(base64.b64decode(ami_import.checksum(block)), hashlib.sha256(block).digest())


if __name__ == "__main__":
    unittest.main()
