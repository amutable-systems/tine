#!/usr/bin/python3
"""Assemble the vendored crate tree that a locked, offline cargo build reads.

Each crate directory carries a .cargo-checksum.json recording the tarball's hash and every unpacked
file's, which is how cargo satisfies itself that the tree matches the lock. The tarball's hash is
recomputed here from bytes buck already verified against the lock, so it necessarily equals the
lock's pin.

Only registry crates are vendored; a git dependency stays a git source, pointed at its fetched
repository instead (see build.py).
"""

import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import TypedDict

import specs


class Spec(TypedDict):
    # The downloaded .crate tarballs, one <name>-<version>.crate each.
    crates: str
    out: str


def _write_checksums(unpacked: Path, package: str) -> None:
    """Hash the tarball and every unpacked file, the way `cargo vendor` records them."""
    files = {
        str(path.relative_to(unpacked)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(unpacked.rglob("*"))
        if path.is_file()
    }
    checksums = {"files": files, "package": package}
    (unpacked / ".cargo-checksum.json").write_text(json.dumps(checksums, sort_keys=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "cargo-vendor", argv)
    out = Path(spec["out"])
    out.mkdir(parents=True, exist_ok=True)

    for archive_path in sorted(Path(spec["crates"]).glob("*.crate")):
        directory = archive_path.name.removesuffix(".crate")
        # Unpack beside the tree rather than into it: a published crate is expected to hold one
        # package, and only the directory it names may become one of cargo's sources.
        with tempfile.TemporaryDirectory(prefix="cargo-vendor.") as scratch:
            with tarfile.open(archive_path) as archive:
                archive.extractall(scratch, filter="data")
            shutil.copytree(Path(scratch) / directory, out / directory)
        _write_checksums(out / directory, package=hashlib.sha256(archive_path.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
