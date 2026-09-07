"""Tests for the Go build driver's checks.

    buck test tine//go:test

Exercise writable source views, package selection, and build commands. The box carries no Go toolchain.
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import build


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
