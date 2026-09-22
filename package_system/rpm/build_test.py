"""Tests for the RPM build driver."""

import contextlib
import errno
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast, override
from unittest import mock

import build


class BuildRpm(unittest.TestCase):
    """Exercise the archive and prepared-source-tree build modes."""

    @override
    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="tine-rpm-build-test-", dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, self.scratch)
        self.spec_file = self.scratch / "example.spec"
        self.spec_file.write_text("Name: example\n")
        # Where Buck would run the driver: the declared outputs live in it, scratch does not.
        self.project = self.scratch / "project"
        self.project.mkdir()
        self.auxiliary = self.scratch / "auxiliary"
        self.auxiliary.write_text("packaging input\n")

    def specification(
        self,
        source_tree: Path | None = None,
        *,
        build_dir: Path | None = None,
        spec_file: Path | None = None,
        subpackages: list[str] | None = None,
    ) -> build.Spec:
        """Return a complete driver spec for one test build."""
        return build.Spec(
            build_dir=str(build_dir) if build_dir is not None else None,
            lower=["buildroot"],
            spec_file=str(spec_file or self.spec_file),
            sources=[str(self.auxiliary)],
            dist=".test",
            source_date_epoch=1234567890,
            release="7",
            out=str(self.project / "out"),
            subpackages=subpackages or [],
            subpackages_out=str(self.project / "subpackages-out"),
            in_place_rpmbuild_options=["--define", "local_option yes"],
            rpmbuild_options=["--define", "common_option yes"],
            source_tree=str(source_tree) if source_tree is not None else None,
        )

    def invoke(
        self, spec: build.Spec, inspect: Callable[[Path], None] | None = None
    ) -> tuple[list[str], Path | None, dict[str, str], Path, list[tuple[str | Path, str | Path]]]:
        """Run the driver with its isolation and rpmbuild process observed."""
        topdir = self.scratch / "topdir"
        completed = subprocess.CompletedProcess[str]([], 0)

        def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            # The project is read-only while rpmbuild runs, for a relative path as well.
            for path in (self.project / "probe", Path("probe")):
                with self.assertRaises(OSError) as caught:
                    path.touch()
                self.assertEqual(caught.exception.errno, errno.EROFS)
            if inspect is not None:
                inspect(topdir)
            return completed

        with (
            contextlib.chdir(self.project),
            mock.patch.object(build.rootfs, "rootfs", return_value=contextlib.nullcontext()) as mounted,
            mock.patch.object(build.subprocess, "run", side_effect=run) as process,
            # Patch the module wrapper first so TemporaryDirectory still uses the real rmtree.
            mock.patch.object(build, "shutil", mock.Mock(wraps=shutil)),
            mock.patch.object(build.shutil, "rmtree") as remove,
        ):
            self.assertEqual(build.build_rpm(spec, topdir), 0)
        remove.assert_called_once_with(topdir)
        (self.project / "probe").touch()
        command = cast(list[str], process.call_args.args[0])
        cwd = cast(Path | None, process.call_args.kwargs["cwd"])
        environment = cast(dict[str, str], process.call_args.kwargs["env"])
        binds = cast(list[tuple[str | Path, str | Path]], mounted.call_args.kwargs["binds"])
        return command, cwd, environment, topdir, binds

    def test_archive_build_runs_prep_and_produces_an_srpm(self) -> None:
        command, cwd, environment, topdir, binds = self.invoke(self.specification())

        self.assertIn("-ba", command)
        self.assertNotIn("--build-in-place", command)
        self.assertNotIn("local_option yes", command)
        self.assertNotIn("_binary_payload w.ufdio", command)
        self.assertNotIn("_source_payload w.ufdio", command)
        self.assertIsNone(cwd)
        self.assertEqual(environment["SOURCE_DATE_EPOCH"], "1234567890")
        self.assertNotIn("_annotated_build", command)
        self.assertEqual(binds, [(topdir, "/build")])
        self.assertEqual((topdir / "SOURCES" / "auxiliary").read_text(), "packaging input\n")
        frozen = (topdir / "SPECS" / "example.spec").read_text()
        self.assertTrue(frozen.startswith("%global autorelease 7%{?dist}\n%global autochangelog %{nil}\n"))

    def test_source_tree_builds_in_place_from_a_disposable_overlay(self) -> None:
        source = self.scratch / "checkout"
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "payload").write_text("local checkout\n")
        (source / "nested" / "parent").symlink_to("..")

        def inspect(topdir: Path) -> None:
            self.assertEqual((topdir / "CHECKOUT" / "nested" / "payload").read_text(), "local checkout\n")
            self.assertEqual((topdir / "CHECKOUT" / "nested" / "parent").readlink(), Path(".."))

        command, cwd, _environment, topdir, binds = self.invoke(self.specification(source), inspect)

        self.assertIn("-bb", command)
        self.assertIn("--noprep", command)
        self.assertIn("--build-in-place", command)
        self.assertIn("common_option yes", command)
        self.assertIn("local_option yes", command)
        self.assertNotIn("_binary_payload w.ufdio", command)
        self.assertNotIn("_source_payload w.ufdio", command)
        self.assertNotIn("_lto_cflags", command)
        self.assertNotIn("_annotated_build", command)
        self.assertNotIn("debug_package %{nil}", command)
        self.assertNotIn("-ba", command)
        self.assertEqual(command[-1], "/build/SPECS/example.spec")
        self.assertEqual(binds, [(topdir, "/build")])
        self.assertEqual(cwd, Path("/build/CHECKOUT"))
        self.assertEqual(list((topdir / "CHECKOUT").iterdir()), [])
        self.assertEqual((topdir / "SOURCES" / "auxiliary").read_text(), "packaging input\n")
        self.assertEqual((source / "nested" / "payload").read_text(), "local checkout\n")

    def test_in_place_spec_and_extras_come_from_the_source_tree(self) -> None:
        source = self.scratch / "checkout"
        packaging = source / "packaging" / "fedora"
        packaging.mkdir(parents=True)
        local_spec = packaging / "local.spec"
        local_spec.write_text("Name: from-checkout\nSource1: helper\n")
        (packaging / "helper").write_text("checkout helper\n")

        specification = self.specification(source, spec_file=local_spec)

        def inspect(topdir: Path) -> None:
            frozen = (topdir / "CHECKOUT" / "packaging" / "fedora" / "local.spec").read_text()
            self.assertTrue(
                frozen.startswith("%global autorelease 7%{?dist}\n%global autochangelog %{nil}\n")
            )
            self.assertIn("Name: from-checkout\n", frozen)
            self.assertEqual(
                (topdir / "CHECKOUT" / "packaging" / "fedora" / "helper").read_text(),
                "checkout helper\n",
            )

        command, cwd, _environment, topdir, _binds = self.invoke(specification, inspect)

        self.assertEqual(command[-1], "/build/CHECKOUT/packaging/fedora/local.spec")
        self.assertIn("_sourcedir /build/CHECKOUT/packaging/fedora", command)
        self.assertEqual(cwd, Path("/build/CHECKOUT"))
        self.assertEqual(list((topdir / "CHECKOUT").iterdir()), [])
        self.assertFalse((topdir / "SOURCES" / "auxiliary").exists())
        self.assertEqual(local_spec.read_text(), "Name: from-checkout\nSource1: helper\n")

    def test_spec_path_cannot_escape_the_source_tree(self) -> None:
        source = self.scratch / "checkout"
        source.mkdir()
        spec = self.specification(source, spec_file=source / ".." / self.spec_file.name)
        with self.assertRaisesRegex(SystemExit, "RPM spec must stay within the source tree"):
            build.build_rpm(spec, self.scratch / "topdir")
        self.assertEqual(self.spec_file.read_text(), "Name: example\n")

    def test_missing_spec_has_a_useful_error(self) -> None:
        source = self.scratch / "checkout"
        source.mkdir()
        for spec_file in (source / "missing.spec", self.scratch / "missing.spec"):
            with self.subTest(spec_file=spec_file):
                spec = self.specification(source, spec_file=spec_file)
                topdir = Path(tempfile.mkdtemp(dir=self.scratch))
                with self.assertRaisesRegex(SystemExit, "RPM spec does not exist: .*missing.spec"):
                    build.build_rpm(spec, topdir)

    def test_packages_cross_into_the_mounted_outputs(self) -> None:
        def produce(topdir: Path) -> None:
            built = topdir / "RPMS/x86_64"
            built.mkdir(parents=True)
            (built / "example-1-7.test.x86_64.rpm").write_text("package\n")

        self.invoke(self.specification(subpackages=["example"]), produce)

        out = self.project / "out"
        self.assertEqual((out / "example-1-7.test.x86_64.rpm").read_text(), "package\n")
        self.assertEqual((self.project / "subpackages-out/example.rpm").read_text(), "package\n")

    def test_dev_source_keeps_build_state_and_uses_the_incremental_profile(self) -> None:
        source = self.scratch / "checkout"
        source.mkdir()
        build_dir = self.scratch / "incremental"
        build_dir.mkdir()
        (build_dir / "cached-object").write_text("keep\n")
        out = self.project / "out"
        out.mkdir()
        (out / "stale.rpm").write_text("old\n")
        subpackages = self.project / "subpackages-out"
        subpackages.mkdir()
        (subpackages / "stale.rpm").write_text("old\n")

        command, _cwd, _environment, topdir, binds = self.invoke(
            self.specification(source, build_dir=build_dir)
        )

        self.assertIn("lto", command)
        self.assertIn("_lto_cflags", command)
        self.assertIn("_annotated_build", command)
        self.assertIn("debug_package %{nil}", command)
        self.assertIn("_binary_payload w.ufdio", command)
        self.assertIn("_source_payload w.ufdio", command)
        self.assertIn("_vpath_builddir /build/BUILD", command)
        self.assertIn("local_option yes", command)
        self.assertLess(command.index("debug_package %{nil}"), command.index("local_option yes"))
        self.assertEqual(binds, [(topdir, "/build")])
        self.assertEqual((build_dir / "cached-object").read_text(), "keep\n")
        self.assertFalse((out / "stale.rpm").exists())
        self.assertFalse((subpackages / "stale.rpm").exists())

    def test_dev_archive_uses_the_incremental_profile(self) -> None:
        build_dir = self.scratch / "incremental"

        command, cwd, _environment, topdir, binds = self.invoke(self.specification(build_dir=build_dir))

        self.assertIn("-ba", command)
        self.assertNotIn("--build-in-place", command)
        self.assertIn("_lto_cflags", command)
        self.assertIn("_annotated_build", command)
        self.assertIn("debug_package %{nil}", command)
        self.assertIn("_binary_payload w.ufdio", command)
        self.assertIn("_source_payload w.ufdio", command)
        self.assertIn("_vpath_builddir /build/BUILD", command)
        self.assertNotIn("local_option yes", command)
        self.assertIsNone(cwd)
        self.assertEqual(binds, [(topdir, "/build")])

    def test_source_overlay_is_disposable_even_after_failure(self) -> None:
        self.check_source_overlay(persistent=False)

    def test_persistent_source_overlay_is_disposable_even_after_failure(self) -> None:
        self.check_source_overlay(persistent=True)

    def check_source_overlay(self, *, persistent: bool) -> None:
        source = self.scratch / "checkout"
        source.mkdir()
        original = source / "original"
        original.write_text("checkout\n")
        os.utime(original, ns=(1234567890123456789, 1234567890123456789))
        (source / "link").symlink_to("original")
        (source / "readonly").mkdir(mode=0o555)
        (source / ".wh.literal").write_text("ordinary checkout file\n")
        before = original.stat()
        build_dir = self.scratch / "persistent" if persistent else None
        spec = self.specification(source, build_dir=build_dir)
        for index, outcome in enumerate((0, 1, RuntimeError("interrupted"), 0)):
            with self.subTest(outcome=outcome):
                topdir = self.scratch / f"topdir-{index}"

                def run(
                    *_args: object,
                    stage: Path = topdir,
                    iteration: int = index,
                    result: int | RuntimeError = outcome,
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    merged = stage / "CHECKOUT"
                    self.assertEqual((merged / "original").stat().st_mtime_ns, before.st_mtime_ns)
                    self.assertEqual((merged / "link").readlink(), Path("original"))
                    self.assertTrue((merged / ".wh.literal").is_file())
                    self.assertFalse((merged / "generated").exists())
                    self.assertEqual((merged / "readonly").stat().st_mode & 0o777, 0o555)
                    (merged / "link").write_text("RPM changed this\n")
                    (merged / "original").unlink()
                    (merged / "generated").write_text("discard\n")
                    (merged / "readonly").chmod(0o700)
                    if build_dir is not None:
                        state = build_dir / "state"
                        self.assertEqual(state.read_text() if state.exists() else "0", str(iteration))
                        state.write_text(str(iteration + 1))
                    if isinstance(result, RuntimeError):
                        raise result
                    return subprocess.CompletedProcess([], result)

                with (
                    mock.patch.object(build.rootfs, "rootfs", return_value=contextlib.nullcontext()),
                    mock.patch.object(build.subprocess, "run", side_effect=run),
                    mock.patch.object(build.shutil, "copytree", side_effect=AssertionError("source copy")),
                ):
                    if isinstance(outcome, RuntimeError):
                        with self.assertRaisesRegex(RuntimeError, "interrupted"):
                            build.build_rpm(spec, topdir)
                    else:
                        self.assertEqual(build.build_rpm(spec, topdir), outcome)
                self.assertEqual(original.read_text(), "checkout\n")
                self.assertEqual(original.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual((source / "readonly").stat().st_mode & 0o777, 0o555)
                self.assertFalse((source / "generated").exists())
                if outcome == 0:
                    self.assertFalse(topdir.exists())
                else:
                    self.assertEqual(list((topdir / "CHECKOUT").iterdir()), [])

    def test_spec_symlink_cannot_write_outside_the_staged_checkout(self) -> None:
        source = self.scratch / "checkout"
        source.mkdir()
        (source / "escape.spec").symlink_to(self.spec_file)
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                topdir = self.scratch / f"topdir-{persistent}"
                spec = self.specification(
                    source,
                    build_dir=self.scratch / "persistent" if persistent else None,
                    spec_file=source / "escape.spec",
                )
                with self.assertRaisesRegex(SystemExit, "spec symlink escapes"):
                    build.build_rpm(spec, topdir)
                self.assertEqual(self.spec_file.read_text(), "Name: example\n")
                self.assertEqual(list((topdir / "CHECKOUT").iterdir()), [])

    def test_capture_makes_build_trees_removable_after_unmount(self) -> None:
        for persistent in (False, True):
            for outcome in (0, 1, RuntimeError("rpmbuild interrupted")):
                with self.subTest(persistent=persistent, outcome=outcome):
                    case = Path(tempfile.mkdtemp(dir=self.scratch))
                    topdir = case / "topdir"
                    build_dir = case / "incremental" if persistent else None
                    directories = [topdir / "BUILDROOT" / "usr"]
                    if build_dir is not None:
                        directories.append(build_dir / "installed")

                    def run(
                        *_args: object,
                        trees: list[Path] = directories,
                        result: int | RuntimeError = outcome,
                        **_kwargs: object,
                    ) -> subprocess.CompletedProcess[str]:
                        for directory in trees:
                            directory.mkdir(parents=True)
                            (directory / "payload").write_text("keep\n")
                            directory.chmod(0o555)
                        if isinstance(result, RuntimeError):
                            raise result
                        return subprocess.CompletedProcess([], result)

                    @contextlib.contextmanager
                    def mounted(trees: list[Path] = directories) -> Iterator[None]:
                        try:
                            yield
                        finally:
                            for directory in trees:
                                self.assertEqual(directory.stat().st_mode & 0o777, 0o555)

                    with (
                        mock.patch.object(build.rootfs, "rootfs", return_value=mounted()),
                        mock.patch.object(build.subprocess, "run", side_effect=run),
                        mock.patch.object(build.shutil, "rmtree") as remove,
                    ):
                        spec = self.specification(build_dir=build_dir)
                        if isinstance(outcome, RuntimeError):
                            with self.assertRaises(RuntimeError) as raised:
                                build.build_rpm(spec, topdir)
                            self.assertIs(raised.exception, outcome)
                        else:
                            self.assertEqual(build.build_rpm(spec, topdir), outcome)

                    for directory in directories:
                        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
                        self.assertEqual((directory / "payload").read_text(), "keep\n")
                    if outcome == 0:
                        remove.assert_called_once_with(topdir)
                    else:
                        remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
