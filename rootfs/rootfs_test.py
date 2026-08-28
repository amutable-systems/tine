"""Tests for root filesystem output capture."""

import unittest
from pathlib import Path
from unittest import mock

import rootfs


class TestCaptureOnExit(unittest.TestCase):
    def test_captures_after_success(self) -> None:
        output = Path("output")
        with mock.patch.object(rootfs, "capture") as capture:
            with rootfs.capture_on_exit(output):
                pass

        capture.assert_called_once_with(output)

    def test_capture_failure_does_not_hide_body_failure(self) -> None:
        output = Path("output")
        with (
            mock.patch.object(rootfs, "capture", side_effect=OSError("bad name")),
            self.assertRaisesRegex(RuntimeError, "transaction failed") as raised,
        ):
            with rootfs.capture_on_exit(output):
                raise RuntimeError("transaction failed")

        self.assertEqual(
            raised.exception.__notes__,
            ["rootfs capture of output also failed: OSError: bad name"],
        )

    def test_capture_failure_after_success_is_reported(self) -> None:
        with (
            mock.patch.object(rootfs, "capture", side_effect=OSError("bad name")),
            self.assertRaisesRegex(OSError, "bad name"),
            rootfs.capture_on_exit("output"),
        ):
            pass


if __name__ == "__main__":
    unittest.main()
