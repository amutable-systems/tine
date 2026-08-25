"""Tests for reading repart's partition report back.

    buck test tine//image_format:test

repart itself is not run here.
"""

import tempfile
import unittest
from pathlib import Path

import repart

HASH = "d3b20557d33fdb5d496b6faec48fbdf517db6f428dae1c362b59c16dac405bfe"


class TestWriteRootHash(unittest.TestCase):
    def _write(self, rows: list[dict[str, object]]) -> str:
        with tempfile.TemporaryDirectory(prefix="repart_test.") as scratch:
            output = Path(scratch) / "roothash"
            repart.write_root_hash(rows, output)
            return output.read_text()

    def test_writes_the_generated_hash(self) -> None:
        # The data partition carries no hash of its own and must not count.
        content = self._write([{"type": "usr-x86-64"}, {"type": "usr-x86-64-verity", "roothash": HASH}])
        self.assertEqual(content, HASH + "\n")

    def test_ignores_the_placeholder(self) -> None:
        content = self._write([{"roothash": "TBD"}, {"roothash": HASH}])
        self.assertEqual(content, HASH + "\n")

    def test_refuses_no_generated_hash(self) -> None:
        with self.assertRaises(SystemExit):
            self._write([{"roothash": "TBD"}, {"type": "esp"}])

    def test_refuses_more_than_one_hash(self) -> None:
        with self.assertRaises(SystemExit):
            self._write([{"roothash": HASH}, {"roothash": HASH.replace("d", "0")}])


if __name__ == "__main__":
    unittest.main()
