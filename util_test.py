"""Tests for the helpers tine's Python entry points share.

buck test tine//:test
"""

import tempfile
import unittest
from pathlib import Path

import util


def scratch(case: unittest.TestCase) -> Path:
    tmp = tempfile.TemporaryDirectory(prefix="util-test.")
    case.addCleanup(tmp.cleanup)
    return Path(tmp.name)


class TestWriteIfChanged(unittest.TestCase):
    def test_leaves_an_unchanged_file_alone(self) -> None:
        path = scratch(self) / "generated"
        util.write_if_changed(path, "one\n")
        before = path.stat().st_mtime_ns
        util.write_if_changed(path, "one\n")
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_replaces_a_changed_file(self) -> None:
        path = scratch(self) / "generated"
        util.write_if_changed(path, "one\n")
        util.write_if_changed(path, "two\n")
        self.assertEqual(path.read_text(), "two\n")

    def test_a_file_reached_through_a_symlink_is_written_through(self) -> None:
        # Buck reads that layout, and renaming over the link would leave the real file stale.
        root = scratch(self)
        shared = root / "shared.bcfg"
        shared.write_text("one\n")
        link = root / "generated"
        link.symlink_to(shared)
        util.write_if_changed(link, "two\n")
        self.assertTrue(link.is_symlink())
        self.assertEqual(shared.read_text(), "two\n")

    def test_reports_a_path_it_cannot_write(self) -> None:
        # Root, which the box runs as, writes through a read-only directory; an absent one stops it.
        with self.assertRaisesRegex(SystemExit, "tine: cannot write"):
            util.write_if_changed(scratch(self) / "absent" / "generated", "one\n")
