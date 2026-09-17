# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Exercise the cargo driver's read-only project and outputs without a Rust toolchain."""

import errno
import os
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from contextlib import chdir
from pathlib import Path
from typing import override
from unittest.mock import patch

import build

type Run = Callable[[list[str | Path], Path, dict[str, str]], None]


class TestBuildCargo(unittest.TestCase):
    scratch: Path

    @override
    def setUp(self) -> None:
        self.root = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="cargo-build-test.", dir="/var/tmp"))
        )
        self.project = self.root / "project"
        self.source = self.project / "checkout/nested"
        self.source.mkdir(parents=True)
        (self.source / "Cargo.toml").write_text('[package]\nname = "example"\n', encoding="utf-8")
        (self.source / "source.rs").write_text("original", encoding="utf-8")
        (self.project / "outside").write_text("outside", encoding="utf-8")
        self.runs = 0

    def _build(
        self, run: Run, *, root: str = "nested", src: str = "checkout", target: str | None = "incremental"
    ) -> None:
        """Run the driver in the fresh scratch space `self.scratch` with `run` in place of cargo."""
        self.runs += 1
        self.scratch = self.root / f"scratch-{self.runs}"
        self.scratch.mkdir()
        spec = build.Spec(
            auditable="cargo-auditable",
            bin="bin",
            binaries=["example"],
            git={},
            root=root,
            src=src,
            target=target,
            vendor="vendor",
        )

        def cargo(command: list[str | Path], *, check: bool, cwd: Path, env: dict[str, str]) -> None:
            self.assertTrue(check)
            run(command, cwd, env)

        with (
            chdir(self.project),
            patch.object(build.subprocess, "run", side_effect=cargo),
            patch.object(tempfile, "tempdir", str(self.scratch)),
        ):
            build.build_cargo(spec)

    @staticmethod
    def _link(env: dict[str, str], content: str) -> None:
        binary = Path(env["CARGO_TARGET_DIR"]) / "release/example"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(content, encoding="utf-8")
        binary.chmod(0o755)

    def test_sources_are_read_only(self) -> None:
        def run(command: list[str | Path], cwd: Path, env: dict[str, str]) -> None:
            self.assertEqual(cwd, self.scratch / "build/nested")
            self.assertEqual((cwd / "source.rs").read_text(encoding="utf-8"), "original")
            self.assertTrue(Path(env["CARGO_HOME"]).is_absolute())
            self.assertTrue(Path(env["CARGO_TARGET_DIR"]).is_absolute())
            for path in (cwd / "source.rs", cwd / "generated", Path("outside")):
                with self.subTest(path=path), self.assertRaises(OSError) as caught:
                    path.write_text("changed", encoding="utf-8")
                self.assertEqual(caught.exception.errno, errno.EROFS)
            self._link(env, "built")

        self._build(run)

        (self.source / "source.rs").write_text("writable again", encoding="utf-8")

    def test_target_is_kept_and_binaries_are_replaced(self) -> None:
        def run(command: list[str | Path], cwd: Path, env: dict[str, str], *, content: str) -> None:
            state = Path(env["CARGO_TARGET_DIR"]) / "state"
            state.write_text(str(int(state.read_text()) + 1) if state.exists() else "1", encoding="utf-8")
            self._link(env, content)

        self._build(lambda command, cwd, env: run(command, cwd, env, content="first"))
        binary = self.project / "bin/example"
        consumer = self.root / "consumer"
        os.link(binary, consumer)
        (self.project / "bin/undeclared").touch()

        def fail(command: list[str | Path], cwd: Path, env: dict[str, str]) -> None:
            run(command, cwd, env, content="broken")
            raise subprocess.CalledProcessError(1, command)

        with self.assertRaises(subprocess.CalledProcessError):
            self._build(fail)
        self.assertFalse(binary.exists())

        self._build(lambda command, cwd, env: run(command, cwd, env, content="second"))
        self.assertEqual(binary.read_text(encoding="utf-8"), "second")
        self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
        self.assertEqual(consumer.read_text(encoding="utf-8"), "first")
        self.assertFalse((self.project / "bin/undeclared").exists())
        self.assertEqual((self.project / "incremental/state").read_text(encoding="utf-8"), "3")

    def test_without_target_cargo_builds_in_scratch(self) -> None:
        def run(command: list[str | Path], cwd: Path, env: dict[str, str]) -> None:
            self.assertEqual(Path(env["CARGO_TARGET_DIR"]), self.scratch / "target")
            self._link(env, "built")

        self._build(run, target=None)
        self.assertEqual((self.project / "bin/example").read_text(encoding="utf-8"), "built")
        self.assertFalse((self.project / "incremental").exists())

    def test_cargo_runs_outside_the_project(self) -> None:
        def run(command: list[str | Path], cwd: Path, env: dict[str, str]) -> None:
            self.assertEqual(cwd, self.scratch / "build")
            self._link(env, "built")

        self._build(run, root="", src="checkout/nested")

    def test_builds_offline_without_git_sources(self) -> None:
        def run(command: list[str | Path], cwd: Path, env: dict[str, str]) -> None:
            self.assertIn("--offline", command)
            self._link(env, "built")

        self._build(run)

    def test_rejects_a_cargo_config_above_the_workspace(self) -> None:
        (self.project / "checkout/.cargo").mkdir()
        (self.project / "checkout/.cargo/config.toml").touch()

        def run(command: list[str | Path], cwd: Path, env: dict[str, str]) -> None:
            self.fail("cargo ran despite the checkout's own configuration")

        with self.assertRaises(SystemExit) as caught:
            self._build(run)
        self.assertEqual(
            str(caught.exception),
            "tine: cargo-build: .cargo/config.toml would override the vendored source configuration; "
            "keep it out of src",
        )


class TestCargoConfig(unittest.TestCase):
    def test_points_sources_at_the_vendored_tree_and_the_fetched_repositories(self) -> None:
        commit = "0123456789abcdef0123456789abcdef01234567"
        git: dict[str, build.GitSource] = {
            commit: {
                "fields": {"git": "https://example.com/dep", "rev": "0123456"},
                "repo": "/repos/dep/.git",
            }
        }
        self.assertEqual(
            build._cargo_config(Path("/vendor"), git),
            '[source.crates-io]\nreplace-with = "vendored-sources"\n'
            "\n"
            '[source."git-0123456789ab-upstream"]\n'
            'git = "https://example.com/dep"\n'
            'rev = "0123456"\n'
            'replace-with = "git-0123456789ab"\n'
            "\n"
            "[source.git-0123456789ab]\n"
            'git = "file:///repos/dep/.git"\n'
            f'rev = "{commit}"\n'
            "\n"
            '[source.vendored-sources]\ndirectory = "/vendor"\n',
        )


class TestTakeBinaries(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.built = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="cargo-build-test.", dir="/var/tmp"))
        )

    def test_rejects_a_binary_the_build_did_not_produce(self) -> None:
        (self.built / "hello-cli").write_text("elf", encoding="utf-8")
        (self.built / "hello-cli").chmod(0o755)
        with self.assertRaises(SystemExit) as caught:
            build._take_binaries(self.built, ["hello"], self.built / "out")
        self.assertEqual(
            str(caught.exception),
            "tine: cargo-build: no hello in target/release, which holds: hello-cli",
        )

    def test_rejects_a_build_that_produced_no_release_directory(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build._take_binaries(self.built / "missing", ["hello"], self.built / "out")
        self.assertEqual(
            str(caught.exception), "tine: cargo-build: no hello in target/release, which holds no executable"
        )
