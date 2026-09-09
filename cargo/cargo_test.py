"""Tests for the Cargo.lock reader, the vendored crate tree, and the build driver's checks.

    buck test tine//cargo:test

The lock driver resolves every remote input before Starlark declares its dynamic actions. The vendor
driver runs for real against synthetic .crate tarballs; nothing here needs a network.
"""

import hashlib
import json
import tarfile
import tempfile
import tomllib
import typing
import unittest
from pathlib import Path
from typing import override

import util

import build
import lock
import vendor

ANYHOW_SHA = "a" * 64
LIBC_SHA = "b" * 64

SD_CONF_URL = "https://github.com/mvo5/sd-conf"
SD_CONF_COMMIT = "f8f381feee216fce20a4ed07163836a082bb0830"
SD_CONF = f"git+{SD_CONF_URL}?rev=f8f381fe#{SD_CONF_COMMIT}"

MANIFEST = """\
[package]
name = "hello"
version = "0.1.0"

[dependencies]
libc = "0.2"
"""

DEPLESS_MANIFEST = """\
[package]
name = "console-info-generator"
version = "0.1.0"

[[bin]]
name = "console-info-generator"
path = "src/main.rs"
"""

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

GIT_LOCK = f"""\
version = 4

[[package]]
name = "sd-conf"
version = "0.1.0"
source = "{SD_CONF}"

[[package]]
name = "sd-conf-macros"
version = "0.1.0"
source = "{SD_CONF}"
"""


def _lock(text: str) -> dict[str, typing.Any]:
    """Parse a lock fixture into what the lock driver's JSON hands the dynamic action."""
    return tomllib.loads(text)


class TestCrateDownloads(unittest.TestCase):
    def test_downloads(self) -> None:
        """Both index spellings resolve; the workspace's own member is not a download."""
        self.assertEqual(
            lock.crate_downloads("hello", _lock(LOCK)),
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

    def test_git_sources_are_not_downloads(self) -> None:
        self.assertEqual(lock.crate_downloads("hello", _lock(GIT_LOCK)), [])

    def test_rejects_unsupported_source(self) -> None:
        source = "sparse+https://crates.example.invalid/"
        lock_text = f'[[package]]\nname = "libc"\nversion = "0.2.180"\nsource = "{source}"\n'
        with self.assertRaises(SystemExit) as caught:
            lock.crate_downloads("hello", _lock(lock_text))
        self.assertEqual(
            str(caught.exception),
            f"tine: cargo_package hello: libc 0.2.180: unsupported dependency source {source}",
        )

    def test_rejects_missing_checksum(self) -> None:
        source = "registry+https://github.com/rust-lang/crates.io-index"
        lock_text = f'[[package]]\nname = "libc"\nversion = "0.2.180"\nsource = "{source}"\n'
        with self.assertRaisesRegex(SystemExit, "libc 0.2.180: no checksum"):
            lock.crate_downloads("hello", _lock(lock_text))

    def test_rejects_a_value_that_is_not_a_lock(self) -> None:
        with self.assertRaisesRegex(SystemExit, r"no \[\[package\]\] list"):
            lock.crate_downloads("hello", {"value": "something else"})


class TestGitSources(unittest.TestCase):
    def test_records_each_git_source_once(self) -> None:
        """Two crates from one repository share the one entry recording it."""
        self.assertEqual(
            lock.git_sources("hello", _lock(GIT_LOCK)),
            {SD_CONF_COMMIT: {"git": SD_CONF_URL, "rev": "f8f381fe"}},
        )

    def _source(self, source: str) -> dict[str, dict[str, str]]:
        lock_text = f'[[package]]\nname = "x"\nversion = "0.1.0"\nsource = "{source}"\n'
        return lock.git_sources("hello", _lock(lock_text))

    def test_source_tracking_the_default_branch(self) -> None:
        commit = "d" * 40
        self.assertEqual(
            self._source(f"git+https://example.invalid/x.git#{commit}"),
            {commit: {"git": "https://example.invalid/x.git"}},
        )

    def test_source_with_branch(self) -> None:
        commit = "e" * 40
        self.assertEqual(
            self._source(f"git+https://example.invalid/x?branch=next#{commit}"),
            {commit: {"git": "https://example.invalid/x", "branch": "next"}},
        )

    def test_source_without_a_commit(self) -> None:
        with self.assertRaisesRegex(SystemExit, "git source without a full commit"):
            self._source("git+https://example.invalid/x")

    def test_source_with_a_short_commit(self) -> None:
        with self.assertRaisesRegex(SystemExit, "git source without a full commit"):
            self._source("git+https://example.invalid/x#f8f381fe")

    def test_rejects_two_spellings_of_one_source(self) -> None:
        """One commit under two source IDs would need a replacement stanza the build never writes."""
        commit = "f" * 40
        lock_text = (
            f'[[package]]\nname = "x"\nversion = "0.1.0"\nsource = "git+https://example.invalid/x?branch=main#{commit}"\n'
            f'[[package]]\nname = "y"\nversion = "0.1.0"\nsource = "git+https://example.invalid/x?rev={commit[:8]}#{commit}"\n'
        )
        with self.assertRaisesRegex(SystemExit, "two spellings of one git source"):
            lock.git_sources("hello", _lock(lock_text))


class TestLockDriver(unittest.TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cargo-lock-test.")
        self.addCleanup(tmp.cleanup)
        self.checkout = Path(tmp.name) / "checkout"
        self.checkout.mkdir()

    def test_finds_a_workspace_inside_a_directory_artifact(self) -> None:
        (self.checkout / "Cargo.toml").write_text(MANIFEST, encoding="utf-8")
        (self.checkout / "Cargo.lock").write_text(LOCK, encoding="utf-8")

        self.assertEqual(
            lock.resolve_workspace("hello", {"generated": str(self.checkout)}),
            {
                "crates": lock.crate_downloads("hello", _lock(LOCK)),
                "git": {},
                "root": "generated",
            },
        )

    def test_finds_a_lockless_workspace_inside_a_directory_artifact(self) -> None:
        (self.checkout / "Cargo.toml").write_text(DEPLESS_MANIFEST, encoding="utf-8")

        self.assertEqual(
            lock.resolve_workspace("nodeps", {"generated": str(self.checkout)}),
            {"crates": [], "git": {}, "root": "generated"},
        )

    def test_rejects_several_locks_across_directory_artifacts(self) -> None:
        other = self.checkout.parent / "other"
        other.mkdir()
        (self.checkout / "Cargo.lock").write_text(LOCK, encoding="utf-8")
        (other / "Cargo.lock").write_text(LOCK, encoding="utf-8")

        with self.assertRaisesRegex(SystemExit, "srcs hold several Cargo.lock files"):
            lock.resolve_workspace(
                "hello",
                {"first": str(self.checkout), "second": str(other)},
            )


class TestBuild(unittest.TestCase):
    def _workspace(self, manifest: str) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="cargo-build-test.")
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "Cargo.toml").write_text(manifest, encoding="utf-8")
        return Path(tmp.name)

    def test_a_manifest_with_nothing_to_resolve_needs_no_lock(self) -> None:
        build._reject_unlocked_dependencies(self._workspace(DEPLESS_MANIFEST))

    def test_rejects_a_missing_lock_when_dependencies_are_declared(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build._reject_unlocked_dependencies(self._workspace(MANIFEST))
        self.assertEqual(
            str(caught.exception),
            "tine: cargo-build: [dependencies] without a Cargo.lock; commit the lock cargo writes",
        )

    def test_cargo_config(self) -> None:
        """The registry points at the vendored tree, each git source at its fetched repository."""
        git: dict[str, build.GitSource] = {
            SD_CONF_COMMIT: {
                "fields": {"git": SD_CONF_URL, "rev": "f8f381fe"},
                "repo": "/repos/sd-conf/.git",
            }
        }
        self.assertEqual(
            build._cargo_config(Path("/vendor"), git),
            '[source.crates-io]\nreplace-with = "vendored-sources"\n'
            "\n"
            '[source."git-f8f381feee21-upstream"]\n'
            f'git = "{SD_CONF_URL}"\n'
            'rev = "f8f381fe"\n'
            'replace-with = "git-f8f381feee21"\n'
            "\n"
            "[source.git-f8f381feee21]\n"
            'git = "file:///repos/sd-conf/.git"\n'
            f'rev = "{SD_CONF_COMMIT}"\n'
            "\n"
            '[source.vendored-sources]\ndirectory = "/vendor"\n',
        )

    def test_rejects_a_binary_the_build_did_not_produce(self) -> None:
        built = self._workspace(DEPLESS_MANIFEST)
        (built / "hello-cli").write_text("elf", encoding="utf-8")
        (built / "hello-cli").chmod(0o755)
        with self.assertRaises(SystemExit) as caught:
            util.take_binaries(
                built, {"hello": str(built / "out")}, tool="cargo-build", where="target/release"
            )
        self.assertEqual(
            str(caught.exception),
            "tine: cargo-build: no hello in target/release, which holds: hello-cli",
        )


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
