# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the environment that selects a go_package's own Go toolchain.

buck test tine//go:test
"""

import tempfile
import unittest
from contextlib import chdir
from pathlib import Path

import toolchain


class TestEnv(unittest.TestCase):
    def test_runs_the_toolchain_go_from_any_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="go-toolchain-test.", dir="/var/tmp") as project:
            with chdir(project):
                env = toolchain.env(Path("buck-out/go"), {"PATH": "/usr/bin:/bin"})
        root = Path(project).absolute() / "buck-out/go"
        self.assertEqual(env, {"GOROOT": str(root), "PATH": f"{root}/bin:/usr/bin:/bin"})
