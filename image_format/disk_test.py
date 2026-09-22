# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the repart driver's split-artifact handling.

    buck test tine//image_format:test

repart itself is not run here: what matters is the definition the driver hands it and what the
driver records about the artifact that comes back, both of which decide how a partition is
published.
"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

import disk

UUID = "fb1bb90c-9de1-b90c-8b89-e04b87c721bd"
PUBLISHED = f"AmutableOS_9_x86-64.usr-x86-64.{UUID}.raw"


DEFINITION = {
    "name": "usr",
    "type": "usr",
    "label": "AmutableOS_9",
    "filesystem": "erofs",
    "copy_files": ["/usr:/"],
    "size_min": None,
    "size_max": None,
    "minimize": "best",
    "compression": "zstd",
    "verity": "data",
    "verity_match_key": "usr",
}


class TestSplitName(unittest.TestCase):
    def test_split_names_by_type_and_uuid(self) -> None:
        # systemd-sysupdate reads the UUID back out of the name as @u, so the artifact repart
        # writes is already named the way it is published.
        self.assertIn("SplitName=%t.%U", disk.Definition.parse(DEFINITION).render(split=True).splitlines())

    def test_no_split_name_without_split(self) -> None:
        rendered = disk.Definition.parse(DEFINITION).render(split=False)
        self.assertNotIn("SplitName", rendered)


class TestCopyPartition(unittest.TestCase):
    def _copy(self, *, contents: bytes, raw_size: int) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="disk_test.") as scratch_dir:
            scratch = Path(scratch_dir)
            source = scratch / PUBLISHED
            source.write_bytes(contents)
            blocks = scratch / "out" / "usr.raw"
            metadata = scratch / "out" / "usr.json"
            row = {
                "split_path": str(source),
                "label": "AmutableOS_9",
                "raw_size": raw_size,
                "type": "usr-x86-64",
                "uuid": UUID,
            }
            disk._copy_partition(row, "usr", blocks, metadata)
            self.assertEqual(blocks.stat().st_size, raw_size)
            return cast(dict[str, object], json.loads(metadata.read_text()))

    def test_records_the_published_name(self) -> None:
        metadata = self._copy(contents=b"x" * 512, raw_size=512)
        self.assertEqual(metadata["published"], PUBLISHED)
        self.assertEqual(metadata["type"], "usr-x86-64")
        self.assertEqual(metadata["uuid"], UUID)

    def test_restores_stripped_padding(self) -> None:
        # A signature artifact leaves repart shorter than its partition; CopyBlocks needs it back.
        self._copy(contents=b"signature", raw_size=512)

    def test_refuses_an_artifact_larger_than_its_partition(self) -> None:
        with self.assertRaises(SystemExit):
            self._copy(contents=b"x" * 1024, raw_size=512)

    def test_refuses_a_partition_repart_did_not_split(self) -> None:
        with tempfile.TemporaryDirectory(prefix="disk_test.") as scratch_dir:
            scratch = Path(scratch_dir)
            with self.assertRaises(SystemExit):
                disk._copy_partition({"split_path": "-"}, "usr", scratch / "usr.raw", scratch / "usr.json")


if __name__ == "__main__":
    unittest.main()
