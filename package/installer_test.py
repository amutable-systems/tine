"""Tests for shared package installation root handling."""

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import installer


class TestRun(unittest.TestCase):
    def test_fresh_root_mount_owns_capture_after_an_install_failure(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
            root = Path(scratch)
            target = root / "target"
            spec = installer.InstallSpec(
                arch="x86_64",
                packages_dir=str(root / "packages"),
                target=str(target),
                installroot=None,
                lower=[],
                work=None,
                box_config=False,
                langs=[],
                docs=True,
            )

            def fail(
                packages_dir: Path,
                installroot: Path,
                request: installer.InstallSpec,
                layered: bool,
            ) -> None:
                raise RuntimeError("install failed")

            with (
                mock.patch.object(installer.specs, "parse", return_value=spec),
                mock.patch.object(
                    installer.rootfs,
                    "rootfs",
                    return_value=nullcontext(),
                ) as mounted,
                self.assertRaisesRegex(RuntimeError, "install failed"),
            ):
                installer.run("install", fail, [])

            mounted.assert_called_once_with(
                installer.BUILDROOT,
                bind=target,
                capture_bind=True,
                apivfs=True,
            )


if __name__ == "__main__":
    unittest.main()
