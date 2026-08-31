"""Pin an alpm repository's database and its authoritative package inventory.

Unlike repomd, an alpm database is one file that carries no checksum of its own and no
content-addressed name, so the snapshot pins the bytes this refresh saw. That only stays buildable
against a mirror whose databases are immutable, which is what the Arch Linux Archive's dated trees
are.
"""

import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

import alpm
import snapshotter
from href import relative_href
from snapshotter import MetadataFile, PackageEntry, RepositoryMetadata


class Spec(snapshotter.Spec):
    db: str


def _inventory(rid: str, db: Path) -> dict[str, PackageEntry]:
    """Index a repository database by the content checksum of each package it serves."""
    packages: dict[str, PackageEntry] = {}
    for package in alpm.read_db(db, rid):
        what = f"package {package.id}"
        digest = snapshotter.checksum(rid, f"{what} %SHA256SUM%", package.sha256)
        if package.size <= 0:
            raise SystemExit(f"{rid}: {what} has invalid %CSIZE% {package.size}")
        location = relative_href(rid, what, package.filename)
        if not alpm.is_package(location):
            raise SystemExit(f"{rid}: {what} is not an alpm package: {location!r}")
        snapshotter.add_package(packages, rid, digest, PackageEntry(location=location, size=package.size))
    return packages


def snapshot_repository(spec: Spec) -> Mapping[str, object]:
    """Pin one repository's database and the packages it currently describes."""
    rid, name = spec["id"], spec["db"]
    print(f"{rid}: snapshotting {name}…", file=sys.stderr)
    url = spec["baseurl"].rstrip("/") + "/" + relative_href(rid, "database", name)
    with tempfile.TemporaryDirectory() as scratch:
        db = Path(scratch) / name
        with db.open("w+b") as stream:
            sha256, size = snapshotter.download(rid, "database", url, stream)
        packages = _inventory(rid, db)
    return {
        "metadata": RepositoryMetadata(
            files=[MetadataFile(out=name, url=url, sha256=sha256, size=size)],
            inline={},
        ),
        "packages": packages,
    }


def main(argv: list[str] | None = None) -> None:
    snapshotter.run("snapshot", Spec, snapshot_repository, argv)


if __name__ == "__main__":
    main()
