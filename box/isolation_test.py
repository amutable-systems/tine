"""Exercise scoped mounts inside the test sandbox's mount namespace."""

import errno
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, chdir
from pathlib import Path
from unittest import mock

import isolation


class TestChroot(unittest.TestCase):
    def test_root_and_cwd_are_restored_on_success_and_failure(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            (root / "inside").touch()
            for fail in (False, True):
                with self.subTest(fail=fail):
                    try:
                        with isolation.chroot(root):
                            self.assertEqual(Path.cwd(), Path("/"))
                            self.assertTrue(Path("/inside").exists())
                            if fail:
                                raise RuntimeError("body failed")
                    except RuntimeError as error:
                        self.assertTrue(fail)
                        self.assertEqual(str(error), "body failed")
                    self.assertEqual(Path.cwd(), previous)
                    self.assertTrue((root / "inside").exists())

    def test_failed_chroot_closes_the_root_descriptor(self) -> None:
        with (
            mock.patch.object(os, "open", return_value=123),
            mock.patch.object(os, "chroot", side_effect=OSError("chroot failed")),
            mock.patch.object(os, "close") as close,
            self.assertRaisesRegex(OSError, "chroot failed"),
            isolation.chroot(Path("/missing")),
        ):
            self.fail("failed chroot must not enter the body")
        close.assert_called_once_with(123)

    def test_reentry_does_not_lose_the_saved_root(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            (root / "inside").touch()
            context = isolation.chroot(root)
            with context:
                with self.assertRaisesRegex(RuntimeError, "already entered"), context:
                    self.fail("an entered chroot context must reject reentry")
                self.assertTrue(Path("/inside").exists())
            self.assertEqual(Path.cwd(), previous)
            self.assertTrue((root / "inside").exists())

    def test_failed_chdir_restores_the_root_and_cwd(self) -> None:
        previous = Path.cwd()
        chdir = os.chdir

        def fail_chdir(path: str) -> None:
            if Path(path) == Path("/"):
                raise OSError("chdir failed")
            chdir(path)

        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            (root / "inside").touch()
            with (
                mock.patch.object(os, "chdir", side_effect=fail_chdir),
                self.assertRaisesRegex(OSError, "chdir failed"),
                isolation.chroot(root),
            ):
                self.fail("failed chdir must not enter the body")
            self.assertEqual(Path.cwd(), previous)
            self.assertTrue((root / "inside").exists())


class TestStartup(unittest.TestCase):
    def test_import_does_not_load_heavy_helpers(self) -> None:
        process = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import isolation; "
                "print('\\n'.join(sys.modules))",
                str(Path(isolation.__file__).parent),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        unwanted = {"annotationlib", "dataclasses", "inspect", "pathlib"}
        if sys.version_info >= (3, 14):  # noqa: UP036
            unwanted.add("typing")
        self.assertFalse(unwanted & set(process.stdout.splitlines()))


class TestPaths(unittest.TestCase):
    def test_nofollow_dot_components_do_not_escape_the_supplied_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            (root / "sub").mkdir()
            for path in ("/", "/.", "/..", "/../..", "/sub/..", "/sub/../../"):
                with self.subTest(path=path):
                    self.assertEqual(isolation._resolve(root, path, nofollow=True), str(root))

    def test_absolute_symlinks_resolve_inside_the_supplied_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            (root / "real").mkdir()
            (root / "real/file").touch()
            (root / "alias").symlink_to("/real")
            (root / "real/link").symlink_to("/real/file")

            self.assertEqual(isolation._resolve(root, "/alias/link"), str(root / "real/file"))
            self.assertEqual(isolation._resolve(root, "/alias/link", nofollow=True), str(root / "real/link"))
            self.assertEqual(
                isolation._resolve(root, "/alias/link/", nofollow=True), str(root / "real/link")
            )

    def test_parent_components_are_resolved_after_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            (root / "sub/directory").mkdir(parents=True)
            (root / "jump").symlink_to("sub/directory")
            (root / "sub/file").touch()

            self.assertEqual(isolation._resolve(root, "/jump/../file"), str(root / "sub/file"))

    def test_relative_paths_only_resolve_against_the_current_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory, chdir(directory):
            root = Path(directory)
            (root / "target").touch()
            (root / "alias").symlink_to("target")

            self.assertEqual(isolation._resolve(Path("/"), Path("alias")), str(root / "target"))
            self.assertEqual(isolation._resolve("/", "alias", nofollow=True), str(root / "alias"))
            with self.assertRaisesRegex(ValueError, "must be absolute"):
                isolation._resolve(root, "alias")

    def test_symlink_target_text_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            target = Path(directory) / "link"
            isolation.Symlink("./usr/bin/", target).mount()

            self.assertEqual(os.readlink(target), "./usr/bin/")

    def test_symlink_parents_are_resolved_inside_the_supplied_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root, outside = Path(directory) / "root", Path(directory) / "outside"
            inside = root / outside.relative_to("/")
            inside.mkdir(parents=True)
            outside.mkdir()
            (root / "alias").symlink_to(outside)

            isolation.Symlink("target", "/alias/link").mount(new_root=root)

            self.assertFalse((outside / "link").is_symlink())
            self.assertEqual((inside / "link").readlink(), Path("target"))

    def test_mounts_sort_by_components_and_bind_over_symlinks(self) -> None:
        parent = isolation.Tmpfs("/usr")
        child = isolation.Bind("/source", "/usr/./bin/")
        sibling = isolation.Tmpfs("/usr-bin")
        link = isolation.Symlink("usr/lib", "/lib")
        bind = isolation.Bind("/source", "/lib")

        self.assertEqual(
            sorted([sibling, child, bind, parent, link], key=isolation._filesystem_key),
            [link, bind, parent, child, sibling],
        )


class TestMountContexts(unittest.TestCase):
    def test_overlay_paths_escape_option_separators(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            lower, upper, work = (root / name for name in ("low:er", "up,per", "wo\\rk"))
            for path in (lower, upper, work):
                path.mkdir()
            (lower / "original").write_text("original")
            target = root / "target"
            with isolation.Overlay((lower,), upper, work, target, lazy_unmount=False):
                self.assertEqual((target / "original").read_text(), "original")
                (target / "generated").write_text("output")
            self.assertEqual((upper / "generated").read_text(), "output")

    def test_overlay_parent_keeps_its_mode_with_a_permissive_umask(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            lower, upper, work = (root / name for name in ("lower", "upper", "work"))
            for path in (lower, upper, work):
                path.mkdir()
            target = root / "parent/target"

            with ExitStack() as stack:
                stack.callback(os.umask, os.umask(0))
                stack.enter_context(mock.patch.object(isolation, "mount"))
                isolation.Overlay((lower,), upper, work, target, lazy_unmount=True).mount()

            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_bind_carries_the_mounts_below_its_source(self) -> None:
        # The rpm driver mounts its persistent build directory inside the tree it then binds into
        # the buildroot, and relies on that mount arriving there with it.
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            kept, tree, target = (root / name for name in ("kept", "tree", "target"))
            kept.mkdir()
            with isolation.Bind(kept, tree / "BUILD"), isolation.Bind(tree, target):
                (target / "BUILD/object").write_text("built")
            self.assertEqual((kept / "object").read_text(), "built")
            self.assertEqual(list((tree / "BUILD").iterdir()), [])

    def test_readonly_file_bind_covers_a_symlink_without_touching_its_target(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            source, other, target = (root / name for name in ("source", "other", "target"))
            source.write_text("source")
            other.write_text("other")
            target.symlink_to("other")

            with isolation.Bind(source, target, readonly=True):
                self.assertEqual(target.read_text(), "source")
                with self.assertRaises(OSError) as raised:
                    target.write_text("changed")
                self.assertEqual(raised.exception.errno, errno.EROFS)
                self.assertEqual(other.read_text(), "other")

            self.assertEqual(target.readlink(), Path("other"))
            self.assertEqual(source.read_text(), "source")

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
                # Strict, so that unwinding covers both unmount flavours.
                isolation.Overlay((lower,), upper, work, target, lazy_unmount=False),
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
