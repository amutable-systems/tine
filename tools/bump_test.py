"""Tests for the pin bumper

    buck test tine//tools:bump-test

The GitHub API is stubbed for the release pins, so both kinds of pin are covered offline.
"""

import contextlib
import json
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast, override
from unittest import mock

import bump


def git(repository: Path, *args: str) -> str:
    """Output of a git command in `repository`, with an identity so committing works anywhere.

    Only stdout: what git says when a fixture command fails belongs on the console.
    """
    command = ["git", "-C", str(repository), "-c", "user.name=t", "-c", "user.email=t@e.st", *args]
    run = subprocess.run(command, check=True, stdout=subprocess.PIPE, encoding="utf-8")
    return run.stdout.strip()


class GitPin(unittest.TestCase):
    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repository = self.root / "origin"
        git(self.root, "init", "--quiet", "--initial-branch=main", str(self.repository))
        self.head = self.commit()

    def commit(self) -> str:
        """One more commit on the repository's only branch; its hash."""
        git(self.repository, "commit", "--quiet", "--allow-empty", "--message=a change")
        return git(self.repository, "rev-parse", "HEAD")

    def pin(self, ref: str = "main", commit: str = "0" * 40) -> dict[str, str]:
        return {"repository": str(self.repository), "ref": ref, "commit": commit}

    def test_takes_the_commit_the_ref_points_at(self) -> None:
        pin = self.pin()
        self.assertEqual(bump._bump_ref("hello", pin), ("0" * 12, self.head[:12]))
        self.assertEqual(pin["commit"], self.head)

    def test_leaves_a_pin_that_has_not_moved(self) -> None:
        pin = self.pin(commit=self.head)
        self.assertIsNone(bump._bump_ref("hello", pin))
        self.assertEqual(pin["commit"], self.head)

    def test_follows_the_ref_rather_than_the_default_branch(self) -> None:
        git(self.repository, "branch", "release")
        moved = self.commit()
        release, main = self.pin(ref="release"), self.pin()
        bump._bump_ref("hello", release)
        bump._bump_ref("hello", main)
        self.assertEqual((release["commit"], main["commit"]), (self.head, moved))

    def test_rejects_a_ref_that_matches_twice(self) -> None:
        git(self.repository, "tag", "main")
        with self.assertRaisesRegex(ValueError, "main matches 2 refs"):
            bump._bump_ref("hello", self.pin())

    def test_fails_on_a_ref_that_is_gone(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            bump._bump_ref("hello", self.pin(ref="never-existed"))

    def test_ignores_the_configuration_of_the_checkout_it_runs_in(self) -> None:
        """A pin resolves through the credentials that clone it, not the current checkout's.

        On a runner those differ: actions/checkout persists one in the local configuration that
        reaches the repository being bumped and nothing else. Stand in for it with a local rewrite
        sending the fixture somewhere there is no repository at all.
        """
        enclosing = self.root / "enclosing"
        git(self.root, "init", "--quiet", str(enclosing))
        git(enclosing, "config", f"url.{self.root / 'gone'}.insteadOf", str(self.repository))
        with contextlib.chdir(enclosing):
            self.assertEqual(bump._bump_ref("hello", self.pin()), ("0" * 12, self.head[:12]))

    def test_writes_the_pin_back_to_the_data_file(self) -> None:
        data = self.root / "pins.json"
        data.write_text(json.dumps({"hello": self.pin()}))
        with mock.patch("sys.argv", ["bump", "--data", str(data), "--all"]):
            bump.main()
        self.assertEqual(json.loads(data.read_text())["hello"]["commit"], self.head)


class ReleasePin(unittest.TestCase):
    """The release path with GitHub stubbed out, so only tine's own decisions are under test.

    A release pin is chosen by two things: which asset in the release succeeds the pinned one, and
    (for CPython alone) the minor that the ty configuration pins the interpreter to.
    """

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ty_config = self.root / "ty.toml"
        self.ty_config.write_text('[environment]\npython-version = "3.14"\n')
        self.data = self.root / "tools.json"

    @staticmethod
    def asset(name: str, size: int = 1) -> dict[str, object]:
        # The digest only has to be well-formed: what it hashes is never downloaded here.
        return {"name": name, "size": size, "digest": f"sha256:{'ab' * 32}"}

    @staticmethod
    def syft() -> dict[str, object]:
        """One pin whose artifact embeds the release, and which reads no ty configuration."""
        return {
            "syft": {
                "repository": "anchore/syft",
                "release": "v1.50.0",
                "platforms": {"x86_64": {"artifact": "syft_1.50.0_linux_amd64.tar.gz"}},
            },
        }

    def bump(
        self, pins: Mapping[str, object], release: Mapping[str, object], *, ty_config: bool = True
    ) -> dict[str, Any]:
        """Bump `pins` against one stubbed release; the rewritten file.

        `ty_config` off stands in for a consuming project, whose command passes none.
        """
        self.data.write_text(json.dumps(pins, indent=2) + "\n")
        option = ["--ty-config", str(self.ty_config)] if ty_config else []
        argv = ["bump", "--data", str(self.data), *option, "--all"]
        with mock.patch.object(bump, "_github_json", return_value=release), mock.patch("sys.argv", argv):
            bump.main()
        return cast(dict[str, Any], json.loads(self.data.read_text()))

    def test_reads_the_pinned_python_minor_from_the_ty_config(self) -> None:
        """A CPython asset is matched by minor, so it comes from the file ty resolves imports with.

        Reading it anywhere else would pin an interpreter ty does not check against.
        """
        self.assertEqual(bump._python_minor(self.ty_config), "3.14")

    def test_fails_on_a_ty_config_pinning_no_python(self) -> None:
        self.ty_config.write_text('[rules]\nall = "error"\n')
        with self.assertRaisesRegex(ValueError, "environment table in .*ty.toml"):
            bump._python_minor(self.ty_config)

    def test_reads_pyproject_configuration_for_consuming_projects(self) -> None:
        path = self.root / "pyproject.toml"
        path.write_text('[tool.ty.environment]\npython-version = "3.13"\n')
        self.assertEqual(bump._python_minor(path), "3.13")

    def test_fails_on_a_cpython_pin_with_no_ty_config(self) -> None:
        """Only CPython needs one, so the option is optional and the demand is the pin's."""
        with self.assertRaisesRegex(ValueError, "needs --ty-config"):
            bump._python_minor(None)

    def test_follows_a_cpython_asset_across_patch_and_date(self) -> None:
        pins = {
            "python3": {
                "repository": "astral-sh/python-build-standalone",
                "release": "20260805",
                "platforms": {
                    "x86_64": {
                        "artifact": "cpython-3.14.7+20260805-x86_64-unknown-linux-gnu-install_only.tar.gz",
                        "sha256": "0" * 64,
                        "size": 1,
                        "strip_prefix": "python",
                    },
                },
            },
        }
        release = {
            "tag_name": "20260814",
            "assets": [
                # A newer patch and date on the pinned platform, plus the near misses around it:
                # another minor, another platform, and an earlier patch in the same release.
                self.asset("cpython-3.14.8+20260814-x86_64-unknown-linux-gnu-install_only.tar.gz", 9),
                self.asset("cpython-3.14.6+20260814-x86_64-unknown-linux-gnu-install_only.tar.gz"),
                self.asset("cpython-3.15.0+20260814-x86_64-unknown-linux-gnu-install_only.tar.gz"),
                self.asset("cpython-3.14.8+20260814-aarch64-unknown-linux-gnu-install_only.tar.gz"),
                self.asset("cpython-3.14.8+20260814-x86_64-unknown-linux-gnu-debug-full.tar.gz"),
            ],
        }
        entry = self.bump(pins, release)["python3"]["platforms"]["x86_64"]
        self.assertEqual(
            entry,
            {
                "artifact": "cpython-3.14.8+20260814-x86_64-unknown-linux-gnu-install_only.tar.gz",
                "sha256": "ab" * 32,
                "size": 9,
                "strip_prefix": "python",
            },
        )

    def test_follows_an_asset_named_after_the_release(self) -> None:
        """syft embeds the tag without its leading "v", so both spellings have to wildcard."""
        release = {
            "tag_name": "v1.51.0",
            "assets": [
                self.asset("syft_1.51.0_linux_amd64.tar.gz"),
                self.asset("syft_1.51.0_linux_arm64.tar.gz"),
            ],
        }
        data = self.bump(self.syft(), release)
        self.assertEqual(data["syft"]["release"], "v1.51.0")
        self.assertEqual(data["syft"]["platforms"]["x86_64"]["artifact"], "syft_1.51.0_linux_amd64.tar.gz")

    def test_bumps_a_pin_needing_no_python_with_no_ty_config(self) -> None:
        """What a consuming project does: its own tools.json, and no ty configuration to pass."""
        release = {"tag_name": "v1.51.0", "assets": [self.asset("syft_1.51.0_linux_amd64.tar.gz")]}
        data = self.bump(self.syft(), release, ty_config=False)
        self.assertEqual(data["syft"]["release"], "v1.51.0")

    def test_leaves_a_release_that_has_not_moved(self) -> None:
        pins = {
            "cargo-auditable": {
                "repository": "rust-secure-code/cargo-auditable",
                "release": "v0.7.5",
                "platforms": {
                    "x86_64": {
                        "artifact": "cargo-auditable-x86_64-unknown-linux-musl.tgz",
                        "sha256": "ab" * 32,
                        "size": 1,
                    },
                },
            },
        }
        release = {
            "tag_name": "v0.7.5",
            "assets": [self.asset("cargo-auditable-x86_64-unknown-linux-musl.tgz")],
        }
        original = json.dumps(pins, indent=2) + "\n"
        self.assertEqual(self.bump(pins, release), json.loads(original))
        self.assertEqual(self.data.read_text(), original)

    def test_fails_on_a_release_missing_the_pinned_platform(self) -> None:
        release = {"tag_name": "v1.51.0", "assets": [self.asset("syft_1.51.0_linux_arm64.tar.gz")]}
        with self.assertRaises(SystemExit):
            self.bump(self.syft(), release)


if __name__ == "__main__":
    unittest.main()
