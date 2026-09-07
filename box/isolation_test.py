"""Exercise scoped mounts inside the test sandbox's mount namespace."""

import tempfile
import unittest
from contextlib import ExitStack, chdir
from pathlib import Path
from unittest import mock

import isolation


class TestMountContexts(unittest.TestCase):
    def test_mounts_unwind_on_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            lower, upper, work = (root / name for name in ("lower", "upper", "work"))
            for path in (lower, upper, work):
                path.mkdir()
            target = root / "target"
            target.mkdir()
            (target / "underlying").touch()
            mounts = [
                isolation.Bind(lower, target),
                isolation.Tmpfs(target),
                isolation.Devices(target),
                isolation.Overlay((lower,), upper, work, target),
            ]
            for mount in mounts:
                for fail in (False, True):
                    with self.subTest(mount=type(mount).__name__, fail=fail):
                        try:
                            with mount as entered:
                                self.assertIs(entered, mount)
                                self.assertFalse((target / "underlying").exists())
                                (target / "during").touch()
                                if fail:
                                    raise RuntimeError("body failed")
                        except RuntimeError as error:
                            self.assertTrue(fail)
                            self.assertEqual(str(error), "body failed")
                        self.assertTrue((target / "underlying").exists())
                        self.assertFalse((target / "during").exists())

    def test_unmount_uses_original_target_after_symlink_and_cwd_change(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            target, other = root / "target", root / "other"
            target.mkdir()
            other.mkdir()
            (target / "underlying").touch()
            alias = root / "alias"
            alias.symlink_to("target")
            with ExitStack() as stack:
                stack.enter_context(chdir(root))
                with isolation.Tmpfs(Path("alias")):
                    alias.unlink()
                    alias.symlink_to("other")
                    stack.enter_context(chdir(other))
                    self.assertFalse((target / "underlying").exists())
                self.assertTrue((target / "underlying").exists())

    def test_bind_of_symlink_unmounts_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.symlink_to("missing")
            with isolation.Bind(source, target, nofollow=True):
                self.assertEqual(target.readlink(), Path("missing"))
            self.assertFalse(target.is_symlink())
            self.assertTrue(target.is_file())

    def test_failed_device_setup_unmounts_partial_tree(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            target = Path(directory) / "dev"
            target.mkdir()
            (target / "underlying").touch()
            with (
                mock.patch.object(isolation.Devices, "_populate", side_effect=OSError("device failed")),
                self.assertRaisesRegex(OSError, "device failed"),
                isolation.Devices(target),
            ):
                self.fail("failed mount must not enter the body")
            self.assertTrue((target / "underlying").exists())

    def test_failed_initial_mount_does_not_unmount_existing_tree(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            target = Path(directory)
            with (
                mock.patch.object(isolation, "mount", side_effect=OSError("mount failed")),
                mock.patch.object(isolation, "umount2") as unmount,
                self.assertRaisesRegex(OSError, "mount failed"),
                isolation.Tmpfs(target),
            ):
                self.fail("failed mount must not enter the body")
            unmount.assert_not_called()


if __name__ == "__main__":
    unittest.main()
