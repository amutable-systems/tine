"""Tests for the pin bumper

    buck test tine//tools:bump-test

Only the git pins: a release pin asks the GitHub API, which we can't unit-test.
"""

import contextlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override
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


if __name__ == "__main__":
    unittest.main()
