# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the runner of Go test binaries.

buck test tine//go:test
"""

import errno
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import chdir, redirect_stdout
from pathlib import Path
from typing import override
from unittest.mock import patch

import runner


class TestRunTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.project = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="go-runner-test.", dir="/var/tmp"))
        )
        for package in ("server", "server/api"):
            (self.project / "checkout" / package).mkdir(parents=True, exist_ok=True)
            (self.project / "checkout" / package / "main_test.go").write_text("original", encoding="utf-8")
        (self.project / "tests").mkdir()
        (self.project / "tests" / "manifest.json").write_text(
            json.dumps(
                [
                    {"binary": "0.test", "package": "example.com/server", "dir": "server"},
                    {"binary": "1.test", "package": "example.com/server/api", "dir": "server/api"},
                ]
            ),
            encoding="utf-8",
        )
        self.enterContext(chdir(self.project))

    def _run(self, statuses: list[int]) -> tuple[int, list[tuple[list[str], Path]]]:
        calls: list[tuple[list[str], Path]] = []

        def run(command: list[str | Path], *, check: bool, cwd: Path) -> subprocess.CompletedProcess[str]:
            self.assertFalse(check)
            # The tests must find the project read-only, including the sources.
            with self.assertRaises(OSError) as caught:
                (cwd / "main_test.go").write_text("changed", encoding="utf-8")
            self.assertEqual(caught.exception.errno, errno.EROFS)
            calls.append(([str(part) for part in command], cwd))
            return subprocess.CompletedProcess(command, statuses[len(calls) - 1])

        spec = runner.Spec(args=["-test.short", "-test.timeout=1h"], src="checkout", tests="tests")
        with patch.object(runner.subprocess, "run", side_effect=run), redirect_stdout(io.StringIO()):
            status = runner.run_tests(spec)
        return status, calls

    def test_runs_each_binary_in_its_package_directory(self) -> None:
        status, calls = self._run([0, 0])
        self.assertEqual(status, 0)
        tests = self.project.resolve() / "tests"
        self.assertEqual(
            [(command, cwd.resolve()) for command, cwd in calls],
            [
                (
                    [str(tests / binary), *runner._DEFAULT_ARGS, "-test.short", "-test.timeout=1h"],
                    self.project.resolve() / "checkout" / directory,
                )
                for binary, directory in (("0.test", "server"), ("1.test", "server/api"))
            ],
        )
        self.assertEqual((self.project / "checkout/server/main_test.go").read_text(), "original")

    def test_runs_every_binary_and_fails_when_one_fails(self) -> None:
        status, calls = self._run([1, 0])
        self.assertEqual(status, 1)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
