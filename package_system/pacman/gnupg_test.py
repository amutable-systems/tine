# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for running gpg against a built keyring."""

import contextlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import gnupg


class TestGpg(unittest.TestCase):
    def test_a_captured_failure_leaves_what_gpg_printed_on_stderr(self) -> None:
        with contextlib.ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory(dir="/var/tmp"))
            log = stack.enter_context(tempfile.TemporaryFile(dir="/var/tmp"))
            saved = os.dup(2)
            stack.callback(os.close, saved)
            with contextlib.ExitStack() as redirected:
                redirected.callback(os.dup2, saved, 2)
                os.dup2(log.fileno(), 2)
                with self.assertRaises(subprocess.CalledProcessError):
                    gnupg.gpg(Path(directory), "--not-an-option", capture=True, check=True)
            log.seek(0)
            self.assertIn(b"invalid option", log.read())
