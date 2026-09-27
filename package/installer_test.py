# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for shared package installation root handling."""

import os
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import installer


class TestRun(unittest.TestCase):
    def test_package_scripts_tolerate_unmapped_acl_groups(self) -> None:
        # The test box mounts /var/tmp as tmpfs, so a host mounted with noacl cannot hide this failure.
        for mode in ("fresh", "layered", "mounted"):
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory(dir="/var/tmp") as scratch,
                mock.patch.dict(os.environ, {"SYSTEMD_IN_CHROOT": "0"}),
            ):
                root = Path(scratch)
                target, lower = root / "target", root / "lower"
                target.mkdir()
                lower.mkdir()
                spec = installer.InstallSpec(
                    arch="x86_64",
                    packages_dir=str(root / "packages"),
                    target=None if mode == "mounted" else str(target),
                    installroot=str(target) if mode == "mounted" else None,
                    lower=[str(lower)] if mode == "layered" else [],
                    work=str(root / "work") if mode == "layered" else None,
                    box_config=False,
                    langs=[],
                    docs=True,
                )

                def install(
                    packages_dir: Path,
                    installroot: Path,
                    request: installer.InstallSpec,
                    layered: bool,
                ) -> None:
                    (installroot / "journal").mkdir()
                    # Debian's systemd postinst applies an ACL for adm (GID 4), which a namespace
                    # mapping only root cannot store. tmpfiles tolerates it in a detected chroot.
                    subprocess.run(
                        ["systemd-tmpfiles", "--create", f"--root={installroot}", "-"],
                        input="a+ /journal - - - - group:4:r-x\n",
                        text=True,
                        check=True,
                    )

                with mock.patch.object(installer.specs, "parse", return_value=spec):
                    installer.run("install", install, [])

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
                mock.patch.dict(os.environ),
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
