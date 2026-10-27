# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the snapshot of go.dev's release index, with a fixture in place of the index."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import releases

GOARCH = {"arm64": "arm64", "x86_64": "amd64"}
CURRENT = ["go1.27.3", "go1.26.10"]


def _file(version: str, os: str, arch: str, digest: str = "ab") -> dict[str, object]:
    return {
        "filename": f"{version}.{os}-{arch}.tar.gz",
        "os": os,
        "arch": arch,
        "version": version,
        "sha256": digest * 32,
        "size": 7,
        "kind": "archive",
    }


def _release(version: str) -> dict[str, Any]:
    return {
        "version": version,
        "stable": True,
        "files": [
            {**_file(version, "", ""), "filename": f"{version}.src.tar.gz", "kind": "source"},
            _file(version, "darwin", "arm64", digest="cd"),
            _file(version, "linux", "amd64"),
            _file(version, "linux", "arm64"),
        ],
    }


def _archives(version: str) -> dict[str, object]:
    return {
        arch: {"sha256": "ab" * 32, "size": 7, "url": f"https://go.dev/dl/{version}.linux-{goarch}.tar.gz"}
        for arch, goarch in (("arm64", "arm64"), ("x86_64", "amd64"))
    }


class Snapshot(unittest.TestCase):
    def test_snapshots_the_declared_releases(self) -> None:
        index = [_release(version) for version in ("go1.27.4", "go1.27.3", "go1.26.10", "go1.26.9")]
        self.assertEqual(
            releases.snapshot(GOARCH, CURRENT, index),
            {"go1.26.10": _archives("go1.26.10"), "go1.27.3": _archives("go1.27.3")},
        )

    def test_fails_on_a_release_missing_from_the_index(self) -> None:
        with self.assertRaisesRegex(SystemExit, "does not list go1.26.10"):
            releases.snapshot(GOARCH, CURRENT, [_release("go1.27.3")])

    def test_fails_on_a_release_without_an_archive_for_an_architecture(self) -> None:
        release = _release("go1.26.10")
        release["files"] = [file for file in release["files"] if file["arch"] != "arm64"]
        with self.assertRaisesRegex(SystemExit, "lists no archive go1.26.10.linux-arm64.tar.gz"):
            releases.snapshot(GOARCH, CURRENT, [_release("go1.27.3"), release])

    def test_refuses_a_malformed_digest(self) -> None:
        release = _release("go1.26.10")
        release["files"][2]["sha256"] = "AB" * 32
        with self.assertRaisesRegex(SystemExit, "as the sha256 of go1.26.10.linux-amd64.tar.gz"):
            releases.snapshot(GOARCH, CURRENT, [_release("go1.27.3"), release])

    def test_refuses_a_malformed_size(self) -> None:
        for size in (True, -1, "7"):
            release = _release("go1.26.10")
            release["files"][3]["size"] = size
            with self.subTest(size=size), self.assertRaisesRegex(SystemExit, "as the size of go1.26.10"):
                releases.snapshot(GOARCH, CURRENT, [_release("go1.27.3"), release])

    def _main(self, index: list[dict[str, Any]], *arguments: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "spec.json"
            current = Path(directory) / "current.json"
            current.write_text(json.dumps({release: {} for release in CURRENT}))
            spec.write_text(json.dumps({"current": str(current), "goarch": GOARCH}))
            out = Path(directory) / "go.json"
            with mock.patch.object(releases, "urlopen", return_value=io.BytesIO(json.dumps(index).encode())):
                releases.main(["--spec", str(spec), "--out", str(out), *arguments])
            return out.read_text()

    def test_main_snapshots_the_committed_releases(self) -> None:
        index = [_release(version) for version in ("go1.27.4", "go1.27.3", "go1.26.10")]
        self.assertEqual(
            self._main(index),
            json.dumps(releases.snapshot(GOARCH, CURRENT, index), indent=2, sort_keys=True) + "\n",
        )

    def test_main_advances_to_the_newest_patch_releases(self) -> None:
        index = [_release(version) for version in ("go1.27.4", "go1.27.3", "go1.26.10", "go1.21.0")]
        self.assertEqual(
            sorted(json.loads(self._main(index, "--advance"))),
            ["go1.21.0", "go1.26.10", "go1.27.4"],
        )

    def test_main_reports_each_release_that_an_advance_adds(self) -> None:
        index = [_release(version) for version in ("go1.27.4", "go1.27.3", "go1.26.10", "go1.21.0")]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self._main(index, "--advance")
        self.assertEqual(
            stderr.getvalue().splitlines(),
            ["==> go-releases: adding go1.21.0", "==> go-releases: advancing go1.27.3 to go1.27.4"],
        )


class Advance(unittest.TestCase):
    def test_takes_the_newest_patch_release_of_every_minor_version_from_go_1_21_on(self) -> None:
        listed = [
            "go1.28rc1",
            "go1.27.3",
            "go1.27.10",
            "go1.27.0",
            "go1.27rc2",
            "go1.21.13",
            "go1.20.14",
            "weekly",
        ]
        index = [{"version": version} for version in listed] + ["malformed"]
        self.assertEqual(releases.advance([], index), ["go1.21.13", "go1.27.10"])

    def test_never_moves_a_release_backwards(self) -> None:
        index = [{"version": "go1.26.8"}, {"version": "go1.27.3"}]
        self.assertEqual(releases.advance(["go1.26.9"], index), ["go1.26.9", "go1.27.3"])


if __name__ == "__main__":
    unittest.main()
