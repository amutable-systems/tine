"""Tests for the Cargo.lock reader and the vendored crate tree.

    python3 -m unittest discover -s tine/tests -t tine -v

cargo/lock.bzl is Starlark, but deliberately written in the subset that is also plain Python, so
this suite runs it through exec() with buck's `fail` stubbed out. The vendor driver runs for real
against synthetic .crate tarballs; nothing here needs a network.
"""

import hashlib
import importlib.util
import json
import tarfile
import tempfile
import tomllib
import typing
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import override

TINE_REPO = Path(__file__).resolve().parent.parent


class StarlarkFailure(Exception):
    """Raised where Starlark would fail() the parse."""


def _fail(message: str) -> typing.NoReturn:
    raise StarlarkFailure(message)


def _load_bzl(name: str) -> SimpleNamespace:
    path = TINE_REPO / "cargo" / f"{name}.bzl"
    module: dict[str, typing.Any] = {"fail": _fail, "typing": typing}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module)  # noqa: S102
    return SimpleNamespace(**module)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, TINE_REPO / "cargo" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lock_bzl = _load_bzl("lock")
vendor = _load("vendor")

ANYHOW_SHA = "a" * 64
LIBC_SHA = "b" * 64

LOCK = f"""\
version = 4

[[package]]
name = "hello"
version = "0.1.0"
dependencies = ["libc"]

[[package]]
name = "libc"
version = "0.2.180"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "{LIBC_SHA}"

[[package]]
name = "anyhow"
version = "1.0.100"
source = "sparse+https://index.crates.io/"
checksum = "{ANYHOW_SHA}"
"""


def _lock(text: str) -> dict[str, typing.Any]:
    """Parse a lock fixture the way a `?format=toml` data load hands it to the macro."""
    return tomllib.loads(text)


class TestCrateDownloads(unittest.TestCase):
    def test_downloads(self) -> None:
        """Both index spellings resolve; the workspace's own member is not a download."""
        self.assertEqual(
            lock_bzl.crate_downloads("hello", _lock(LOCK)),
            [
                {
                    "name": "anyhow",
                    "version": "1.0.100",
                    "sha256": ANYHOW_SHA,
                    "url": "https://static.crates.io/crates/anyhow/anyhow-1.0.100.crate",
                },
                {
                    "name": "libc",
                    "version": "0.2.180",
                    "sha256": LIBC_SHA,
                    "url": "https://static.crates.io/crates/libc/libc-0.2.180.crate",
                },
            ],
        )

    def test_rejects_unsupported_source(self) -> None:
        source = "sparse+https://crates.example.invalid/"
        lock = f'[[package]]\nname = "libc"\nversion = "0.2.180"\nsource = "{source}"\n'
        with self.assertRaises(StarlarkFailure) as caught:
            lock_bzl.crate_downloads("hello", _lock(lock))
        self.assertEqual(
            str(caught.exception),
            f"cargo_package hello: libc 0.2.180: unsupported dependency source {source}",
        )

    def test_rejects_git_source(self) -> None:
        source = "git+https://github.com/mvo5/sd-conf?rev=f8f381fe#" + "f" * 40
        lock = f'[[package]]\nname = "sd-conf"\nversion = "0.1.0"\nsource = "{source}"\n'
        with self.assertRaisesRegex(StarlarkFailure, "unsupported dependency source"):
            lock_bzl.crate_downloads("hello", _lock(lock))

    def test_rejects_missing_checksum(self) -> None:
        source = "registry+https://github.com/rust-lang/crates.io-index"
        lock = f'[[package]]\nname = "libc"\nversion = "0.2.180"\nsource = "{source}"\n'
        with self.assertRaisesRegex(StarlarkFailure, "libc 0.2.180: no checksum"):
            lock_bzl.crate_downloads("hello", _lock(lock))

    def test_rejects_a_value_that_is_not_a_lock(self) -> None:
        with self.assertRaisesRegex(StarlarkFailure, r"no \[\[package\]\] list"):
            lock_bzl.crate_downloads("hello", {"value": "something else"})


class TestVendor(unittest.TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cargo-vendor-test.")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.crates = self.tmp / "crates"
        self.crates.mkdir()
        self.out = self.tmp / "vendor"

    def _tarball(self, path: Path, top: str, files: dict[str, str]) -> None:
        """Write a tarball holding `files` below one top-level directory."""
        with tarfile.open(path, "w:gz") as archive:
            for name, content in files.items():
                source = self.tmp / "staging" / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(content, encoding="utf-8")
                archive.add(source, arcname=f"{top}/{name}")

    def _crate(self, top: str, files: dict[str, str]) -> Path:
        path = self.crates / f"{top}.crate"
        self._tarball(path, top, files)
        return path

    def _vendor(self) -> None:
        spec = self.tmp / "vendor.spec.json"
        spec.write_text(
            json.dumps({"crates": str(self.crates), "out": str(self.out)}),
            encoding="utf-8",
        )
        vendor.main(["--spec", str(spec)])

    def test_unpacks_and_records_checksums(self) -> None:
        tarball = self._crate("demo-0.1.0", {"Cargo.toml": 'name = "demo"\n', "src/lib.rs": "// nothing\n"})
        self._vendor()

        unpacked = self.out / "demo-0.1.0"
        self.assertEqual((unpacked / "src" / "lib.rs").read_text(encoding="utf-8"), "// nothing\n")
        checksums = json.loads((unpacked / ".cargo-checksum.json").read_text(encoding="utf-8"))
        self.assertEqual(checksums["package"], hashlib.sha256(tarball.read_bytes()).hexdigest())
        self.assertEqual(
            checksums["files"],
            {
                "Cargo.toml": hashlib.sha256(b'name = "demo"\n').hexdigest(),
                "src/lib.rs": hashlib.sha256(b"// nothing\n").hexdigest(),
            },
        )


if __name__ == "__main__":
    unittest.main()
