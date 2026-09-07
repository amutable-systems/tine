"""Tests for the RPM build driver."""

import contextlib
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
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
        self.auxiliary = self.scratch / "auxiliary"
        self.auxiliary.write_text("packaging input\n")

    def specification(
        self,
        source_tree: Path | None = None,
        *,
        build_dir: Path | None = None,
        spec_file: Path | None = None,
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
            out=str(self.scratch / "out"),
            subpackages={},
            in_place_rpmbuild_options=["--define", "local_option yes"],
            rpmbuild_options=["--define", "common_option yes"],
            source_tree=str(source_tree) if source_tree is not None else None,
        )

    def invoke(
        self, spec: build.Spec
    ) -> tuple[list[str], Path | None, dict[str, str], Path, list[tuple[str | Path, str | Path]]]:
        """Run the driver with its isolation and rpmbuild process observed."""
        topdir = self.scratch / "topdir"
        completed = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.object(build.rootfs, "rootfs", return_value=contextlib.nullcontext()) as mounted,
            mock.patch.object(build.subprocess, "run", return_value=completed) as run,
            mock.patch.object(build.shutil, "rmtree") as remove,
        ):
            self.assertEqual(build.build_rpm(spec, topdir), 0)
        remove.assert_called_once_with(topdir)
        command = cast(list[str], run.call_args.args[0])
        cwd = cast(Path | None, run.call_args.kwargs["cwd"])
        environment = cast(dict[str, str], run.call_args.kwargs["env"])
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

    def test_source_tree_builds_in_place_from_a_disposable_copy(self) -> None:
        source = self.scratch / "checkout"
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "payload").write_text("local checkout\n")
        (source / "nested" / "parent").symlink_to("..")

        command, cwd, _environment, topdir, binds = self.invoke(self.specification(source))

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
        self.assertEqual((topdir / "CHECKOUT" / "nested" / "payload").read_text(), "local checkout\n")
        self.assertEqual((topdir / "CHECKOUT" / "nested" / "parent").readlink(), Path(".."))
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
        command, cwd, _environment, topdir, _binds = self.invoke(specification)

        self.assertEqual(command[-1], "/build/CHECKOUT/packaging/fedora/local.spec")
        self.assertIn("_sourcedir /build/CHECKOUT/packaging/fedora", command)
        self.assertEqual(cwd, Path("/build/CHECKOUT"))
        frozen = (topdir / "CHECKOUT" / "packaging" / "fedora" / "local.spec").read_text()
        self.assertTrue(frozen.startswith("%global autorelease 7%{?dist}\n%global autochangelog %{nil}\n"))
        self.assertIn("Name: from-checkout\n", frozen)
        self.assertEqual(
            (topdir / "CHECKOUT" / "packaging" / "fedora" / "helper").read_text(),
            "checkout helper\n",
        )
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

    def test_dev_source_keeps_build_state_and_uses_the_incremental_profile(self) -> None:
        source = self.scratch / "checkout"
        source.mkdir()
        build_dir = self.scratch / "incremental"
        build_dir.mkdir()
        (build_dir / "cached-object").write_text("keep\n")
        out = self.scratch / "out"
        out.mkdir()
        (out / "stale.rpm").write_text("old\n")

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
        self.assertEqual(binds, [(topdir, "/build"), (build_dir.absolute(), "/build/BUILD")])
        self.assertEqual((build_dir / "cached-object").read_text(), "keep\n")
        self.assertFalse((out / "stale.rpm").exists())

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
        self.assertEqual(binds, [(topdir, "/build"), (build_dir.absolute(), "/build/BUILD")])

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
