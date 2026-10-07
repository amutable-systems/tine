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


class TestRejectUnlockedDependencies(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.workspace = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="cargo-build-test.", dir="/var/tmp"))
        )

    def test_a_manifest_with_nothing_to_resolve_needs_no_lock(self) -> None:
        (self.workspace / "Cargo.toml").write_text('[package]\nname = "nodeps"\n', encoding="utf-8")
        build._reject_unlocked_dependencies(self.workspace)

    def test_rejects_a_missing_lock_when_dependencies_are_declared(self) -> None:
        (self.workspace / "Cargo.toml").write_text(
            '[package]\nname = "hello"\n\n[dependencies]\nlibc = "0.2"\n', encoding="utf-8"
        )
        with self.assertRaises(SystemExit) as caught:
            build._reject_unlocked_dependencies(self.workspace)
        self.assertEqual(
            str(caught.exception),
            "tine: cargo-build: [dependencies] without a Cargo.lock; commit the lock cargo writes",
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
