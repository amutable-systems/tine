#!/usr/bin/python3
"""Generate a deterministic alpm database for a local package repository.

This is `repo-add` without pacman: the entries a database carries come from each package's
`.PKGINFO` plus the size and checksum of the file itself, so a repository of locally built
packages can be indexed by the same driver that runs everywhere else.
"""

import hashlib
import os
import shutil
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import TypedDict

import specs

import alpm

DATABASE = "local.db"

# `.PKGINFO` key -> database key, in the order a database entry lists them.
FIELDS = (
    ("pkgname", "NAME"),
    ("pkgbase", "BASE"),
    ("pkgver", "VERSION"),
    ("pkgdesc", "DESC"),
    ("url", "URL"),
    ("arch", "ARCH"),
    ("builddate", "BUILDDATE"),
    ("packager", "PACKAGER"),
    ("size", "ISIZE"),
    ("license", "LICENSE"),
    ("replaces", "REPLACES"),
    ("group", "GROUPS"),
    ("conflict", "CONFLICTS"),
    ("provides", "PROVIDES"),
    ("depend", "DEPENDS"),
)


class Spec(TypedDict):
    # Individual packages are published under their own name; a directory's packages are
    # published under <dir-index>/ so consumers can map them back to inputs.
    packages: list[str]
    packages_dirs: list[str]
    out: str


def read_pkginfo(package: Path) -> dict[str, list[str]]:
    """Read the metadata alpm stores in a package."""
    with ExitStack() as stack:
        archive = alpm.open_package(stack, package)
        for member in archive:
            if member.name != ".PKGINFO":
                continue
            source = archive.extractfile(member)
            if source is None:
                break
            info: dict[str, list[str]] = {}
            for line in source.read().decode("utf-8").splitlines():
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                info.setdefault(key.strip(), []).append(value.strip())
            return info
    raise SystemExit(f"{package}: no .PKGINFO; not an alpm package")


def _checksum(package: Path) -> str:
    digest = hashlib.sha256()
    with package.open("rb") as raw:
        while chunk := raw.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def entry(name: str, package: Path) -> tuple[str, dict[str, list[str]]]:
    """Describe one package the way a database entry does."""
    info = read_pkginfo(package)
    values: dict[str, list[str]] = {"FILENAME": [name]}
    for source, key in FIELDS:
        if source in info:
            values[key] = info[source]
    values["CSIZE"] = [str(package.stat().st_size)]
    values["SHA256SUM"] = [_checksum(package)]

    described = alpm.package_from_desc(values, "local", str(package))
    return described.id, values


def _link_or_copy(src: Path, dst: Path) -> None:
    # Symlinks would dangle when the repository is rebound into a sandbox.
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def index(entries: list[tuple[str, Path]], out: Path, epoch: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    entries = sorted(entries)
    if not entries:
        raise SystemExit("no packages given")

    # A location is relative to the repository base URL, which is a directory here.
    for name, package in entries:
        _link_or_copy(package, out / name)

    alpm.write_db([entry(name, package) for name, package in entries], out / DATABASE, epoch)
    print(f"index: {len(entries)} packages → {out}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    spec: Spec = specs.parse("index", argv)
    entries = [(Path(package).name, Path(package).absolute()) for package in spec["packages"]]
    for position, directory in enumerate(spec["packages_dirs"]):
        entries += [
            (alpm.local_href(position, package.name), package)
            for package in sorted(Path(directory).absolute().iterdir())
            if alpm.is_package(package.name)
        ]
    # The fallback keeps standalone use possible.
    index(entries, Path(spec["out"]).absolute(), int(os.environ.get("SOURCE_DATE_EPOCH", "0")))


if __name__ == "__main__":
    main()
