# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the Go build driver's checks.

    buck test tine//go:test

Exercise the read-only project, package selection, and build commands. The box carries no Go toolchain.
"""

import errno
import json
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

    def test_sources_are_read_only_and_kept_outputs_survive_failure(self) -> None:
        for iteration, fail in enumerate((False, True, False)):
            with self.subTest(iteration=iteration, fail=fail):
                scratch = self.scratch / str(iteration)
                scratch.mkdir()
                spec = build.Spec(
                    bin=f"bin-{iteration}",
                    cgo=None,
                    cgo_cflags=[],
                    gocache="gocache",
                    linker_flags=[],
                    module_cache_dir=None,
                    packages={"example": "."},
                    root="",
                    src="checkout",
                    tags=[],
                    test_packages=None,
                    tests=None,
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
                    self.assertEqual(cwd.resolve(), self.source.resolve())
                    # The driver runs in the project directory, so `outside` is a path in the project.
                    # The build must find the project read-only, including the sources.
                    for path in (cwd / "main.go", Path("outside")):
                        with self.assertRaises(OSError) as caught:
                            path.write_text("changed", encoding="utf-8")
                        self.assertEqual(caught.exception.errno, errno.EROFS)

                    state = Path(env["GOCACHE"]) / "state"
                    self.assertEqual(state.read_text() if state.exists() else "0", str(index))
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
                    patch.object(tempfile, "tempdir", str(scratch)),
                ):
                    if fail:
                        with self.assertRaises(subprocess.CalledProcessError):
                            build.build_go(spec)
                    else:
                        build.build_go(spec)

                self.assertEqual((self.source / "main.go").read_text(encoding="utf-8"), "original")
                output = self.project / spec["bin"] / "example"
                if fail:
                    self.assertFalse(output.exists())
                else:
                    self.assertFalse(stale.exists())
                    self.assertEqual(output.read_text(encoding="utf-8"), "built")
                    self.assertEqual(output.stat().st_mode & 0o777, 0o755)
                self.assertEqual((self.project / "gocache/state").read_text(), str(iteration + 1))
                (self.project / "outside").write_text("writable again", encoding="utf-8")


class TestCompileTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="go-build-test.", dir="/var/tmp")))
        self.project = root / "project"
        (self.project / "checkout").mkdir(parents=True)
        (root / "scratch").mkdir()
        self.enterContext(chdir(self.project))
        self.enterContext(patch.object(tempfile, "tempdir", str(root / "scratch")))

    def test_writes_a_binary_and_a_manifest_entry_per_package(self) -> None:
        spec = build.Spec(
            bin="bin",
            cgo=None,
            cgo_cflags=[],
            gocache=None,
            linker_flags=["-s"],
            module_cache_dir=None,
            packages={},
            root="",
            src="checkout",
            tags=[],
            test_packages=["./..."],
            tests="tests",
        )
        stale = self.project / "tests" / "7.test"
        stale.parent.mkdir()
        stale.touch()
        commands: list[list[str]] = []

        def run(command: list[str], *, check: bool, cwd: Path, env: dict[str, str]) -> None:
            commands.append(command)
            Path(command[command.index("-o") + 1]).write_text("compiled", encoding="utf-8")

        packages = [("example.com/a", "a"), ("example.com/b/a", "b/a")]
        with (
            patch.object(build, "_test_packages", return_value=packages) as listed,
            patch.object(build.subprocess, "run", side_effect=run),
        ):
            build.build_go(spec)

        self.assertEqual(listed.call_args.args[0], ["./..."])
        self.assertEqual(
            [command[-1] for command in commands],
            ["example.com/a", "example.com/b/a"],
        )
        tests = self.project / "tests"
        self.assertFalse(stale.exists())
        self.assertEqual((tests / "0.test").read_text(encoding="utf-8"), "compiled")
        self.assertEqual(
            json.loads((tests / "manifest.json").read_text(encoding="utf-8")),
            [
                {"binary": "0.test", "package": "example.com/a", "dir": "a"},
                {"binary": "1.test", "package": "example.com/b/a", "dir": "b/a"},
            ],
        )

    def test_compile_command(self) -> None:
        self.assertEqual(
            build._test_command(Path("/var/tmp/tests/0.test"), "example.com/a", ["-s", "-w"]),
            [
                "go",
                "test",
                "-c",
                "-vet=off",
                "-o",
                "/var/tmp/tests/0.test",
                "-ldflags=-s -w",
                "example.com/a",
            ],
        )

    def test_lists_packages_with_tests_relative_to_src(self) -> None:
        src = self.project / "checkout"
        listing = "".join(
            json.dumps(package)
            for package in [
                {"ImportPath": "example.com/a", "Dir": str(src / "server/a"), "TestGoFiles": ["a_test.go"]},
                {"ImportPath": "example.com/b", "Dir": str(src / "server/b")},
                {"ImportPath": "example.com/c", "Dir": str(src / "api"), "XTestGoFiles": ["c_test.go"]},
            ]
        )
        with patch("build.subprocess.run") as run:
            run.return_value.stdout = listing
            self.assertEqual(
                build._test_packages(["./..."], src / "server", src, {}),
                [("example.com/a", "server/a"), ("example.com/c", "api")],
            )

    def test_rejects_test_packages_outside_src(self) -> None:
        listing = json.dumps(
            {
                "ImportPath": "rsc.io/quote",
                "Dir": "/var/tmp/modules/rsc.io/quote",
                "TestGoFiles": ["quote_test.go"],
            }
        )
        with (
            patch("build.subprocess.run") as run,
            self.assertRaisesRegex(SystemExit, "rsc.io/quote is not in src"),
        ):
            run.return_value.stdout = listing
            build._test_packages(["rsc.io/quote"], Path("checkout"), Path("checkout"), {})


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
