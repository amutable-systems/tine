"""Tests for the Go build driver's checks.

    buck test tine//go:test

Exercise writable source views, package selection, and build commands. The box carries no Go toolchain.
"""

import errno
import os
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from typing import override
from unittest.mock import patch

import build


class TestBuildGo(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="go-build-test.", dir="/var/tmp"))
        )
        self.project = self.scratch / "project"
        self.source = self.project / "checkout"
        self.source.mkdir(parents=True)
        (self.source / "main.go").write_text("original", encoding="utf-8")
        (self.project / "outside").write_text("outside", encoding="utf-8")
        (self.source / "outside").symlink_to(self.project / "outside")

    def test_source_writes_are_disposable(self) -> None:
        self.check_build(persistent=False)

    def test_incremental_outputs_survive_success_and_failure(self) -> None:
        self.check_build(persistent=True)

    def check_build(self, *, persistent: bool) -> None:
        for iteration, fail in enumerate((False, True, False)):
            scratch = self.scratch / str(iteration)
            scratch.mkdir()
            spec = build.Spec(
                bin=f"bin-{iteration}",
                cgo=None,
                cgo_cflags=[],
                gocache="gocache" if persistent else None,
                linker_flags=[],
                module_cache_dir=None,
                packages={"example": "."},
                root="",
                src="checkout",
                tags=[],
            )
            stale = self.project / spec["bin"] / "undeclared"
            stale.parent.mkdir()
            stale.touch()

            def run(
                command: list[str],
                *,
                check: bool,
                cwd: Path,
                env: dict[str, str],
                index: int = iteration,
                fails: bool = fail,
            ) -> None:
                self.assertTrue(check)
                (cwd / "main.go").write_text("changed", encoding="utf-8")
                # Through a source symlink, and relative to the project the driver stands in.
                for path in (cwd / "outside", Path("outside")):
                    with self.assertRaises(OSError) as caught:
                        path.write_text("changed", encoding="utf-8")
                    self.assertEqual(caught.exception.errno, errno.EROFS)

                state = Path(env["GOCACHE"]) / "state"
                self.assertEqual(
                    state.read_text() if state.exists() else "0", str(index if persistent else 0)
                )
                state.write_text(str(index + 1), encoding="utf-8")
                if fails:
                    raise subprocess.CalledProcessError(1, command)
                binary = Path(command[command.index("-o") + 1])
                binary.write_text("built", encoding="utf-8")
                binary.chmod(0o755)

            with (
                chdir(self.project),
                patch.object(build, "_package", return_value="example.com/example"),
                patch.object(build.subprocess, "run", side_effect=run),
                patch.dict(os.environ, {"TMPDIR": str(scratch)}),
                patch.object(tempfile, "tempdir", None),
            ):
                if fail:
                    with self.assertRaises(subprocess.CalledProcessError):
                        build.build_go(spec, scratch)
                else:
                    build.build_go(spec, scratch)

            self.assertEqual((self.source / "main.go").read_text(encoding="utf-8"), "original")
            self.assertEqual(list((scratch / "build").iterdir()), [])
            output = self.project / spec["bin"] / "example"
            if fail:
                self.assertFalse(output.exists())
            else:
                self.assertFalse(stale.exists())
                self.assertEqual(output.read_text(encoding="utf-8"), "built")
                self.assertEqual(output.stat().st_mode & 0o777, 0o755)
            if persistent:
                self.assertEqual((self.project / "gocache/state").read_text(), str(iteration + 1))
            (self.project / "outside").write_text("writable again", encoding="utf-8")


class TestBuildCommand(unittest.TestCase):
    def test_output_name_and_linker_flags(self) -> None:
        self.assertEqual(
            build._build_command(Path("/var/tmp/binaries/etcd"), "example.com/server/v3", ["-s", "-w"]),
            ["go", "build", "-o", "/var/tmp/binaries/etcd", "-ldflags=-s -w", "example.com/server/v3"],
        )

    def test_builds_the_selected_package(self) -> None:
        self.assertEqual(
            build._build_command(Path("/var/tmp/binaries/tool"), "example.com/mycmd/v2", []),
            ["go", "build", "-o", "/var/tmp/binaries/tool", "example.com/mycmd/v2"],
        )


class TestPackage(unittest.TestCase):
    def test_resolves_root_relative_and_import_paths(self) -> None:
        for selector in [".", "./cmd/server", "example.com/server/v3"]:
            with self.subTest(selector=selector), patch("build.subprocess.run") as run:
                run.return_value.stdout = '{"ImportPath": "example.com/server/v3", "Name": "main"}'
                self.assertEqual(
                    build._package(selector, Path("workspace"), {"GOFLAGS": "-tags=test"}),
                    "example.com/server/v3",
                )
                run.assert_called_once_with(
                    ["go", "list", "-json=ImportPath,Name", selector],
                    check=True,
                    cwd=Path("workspace"),
                    env={"GOFLAGS": "-tags=test"},
                    stdout=subprocess.PIPE,
                    text=True,
                )

    def test_skips_libraries_beside_the_main_package(self) -> None:
        listing = (
            '{"ImportPath": "example.com/server/lib", "Name": "lib"}\n'
            '{\n\t"ImportPath": "example.com/server/cmd/server",\n\t"Name": "main"\n}\n'
        )
        with patch("build.subprocess.run") as run:
            run.return_value.stdout = listing
            self.assertEqual(build._package("./...", Path("workspace"), {}), "example.com/server/cmd/server")

    def test_rejects_libraries_empty_and_multiple_matches(self) -> None:
        main = '{"ImportPath": "example.com/server", "Name": "main"}'
        for listing in ["", '{"ImportPath": "example.com/lib", "Name": "lib"}', main + main]:
            with self.subTest(listing=listing), patch("build.subprocess.run") as run:
                run.return_value.stdout = listing
                with self.assertRaisesRegex(SystemExit, "must resolve to exactly one main package"):
                    build._package("./...", Path("workspace"), {})

    def test_rejects_flags_files_and_empty_selectors(self) -> None:
        for selector in ["", "-help", "main.go"]:
            with self.subTest(selector=selector), patch("build.subprocess.run") as run:
                with self.assertRaisesRegex(SystemExit, "invalid package selection"):
                    build._package(selector, Path("workspace"), {})
                run.assert_not_called()

    def test_propagates_missing_or_excluded_package_errors(self) -> None:
        with patch("build.subprocess.run", side_effect=subprocess.CalledProcessError(1, "go list")):
            with self.assertRaises(subprocess.CalledProcessError):
                build._package("./missing", Path("workspace"), {})
