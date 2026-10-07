# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the cargo build driver's helpers."""

import tempfile
import unittest
from pathlib import Path
from typing import override

import build


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
