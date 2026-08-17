#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Generate a deterministic Packages index for a local package repository.

This is `apt-ftparchive packages` without apt: what an index states about a package is the
package's own control stanza plus the size and checksum of the file itself, so a repository of
locally built packages can be indexed by the same driver that runs everywhere else.

The stanza is copied out verbatim rather than reformatted. A control field can be folded over
several lines and its name has a spelling of its own, and neither survives a round trip through a
parser that exists to answer questions about a stanza rather than to reproduce one.
"""

import hashlib
import sys
from pathlib import Path
from typing import TypedDict

import deb822
import debfile
import specs
import util


class Spec(TypedDict):
    # Individual packages are published under their own name; a directory's packages are
    # published under <dir-index>/ so consumers can map them back to inputs.
    packages: list[str]
    packages_dirs: list[str]
    out: str


def _sha256(package: Path) -> str:
    with package.open("rb") as raw:
        return hashlib.file_digest(raw, "sha256").hexdigest()


def stanza(name: str, package: Path) -> str:
    """Describe one package the way a Packages index does."""
    described = debfile.control(package)
    fields = list(deb822.stanzas(described.splitlines()))
    if len(fields) != 1:
        util.fail(f"{package.name}: control holds {len(fields)} stanzas, not one")
    deb822.required(fields[0], "package", f"{package.name}: control")
    for stated in ("filename", "size", "sha256"):
        if stated in fields[0]:
            util.fail(f"{package.name}: control already states {stated}, which an index owns")
    return (
        described.strip("\n")
        + f"\nFilename: {name}"
        + f"\nSize: {package.stat().st_size}"
        + f"\nSHA256: {_sha256(package)}\n\n"
    )


def index(entries: list[tuple[str, Path]], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    entries = sorted(entries)
    if not entries:
        util.fail("no packages given")

    # A location is relative to the repository base URL, which is a directory here.
    for name, package in entries:
        # Hardlinked rather than symlinked, which would dangle once the repository is rebound into
        # a sandbox; `clone_file` replaces rather than rewrites, so a kept output is never edited
        # through a link a previous run left behind.
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        util.clone_file(package, out / name, allow_link=True)
    (out / deb822.INDEX).write_text(
        "".join(stanza(name, package) for name, package in entries), encoding="utf-8"
    )
    print(f"index: {len(entries)} packages → {out}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "index", argv)
    entries = [(Path(package).name, Path(package).absolute()) for package in spec["packages"]]
    for position, directory in enumerate(spec["packages_dirs"]):
        entries += [
            (f"{position}/{package.name}", package)
            for package in sorted(Path(directory).absolute().iterdir())
            if package.suffix == ".deb"
        ]
    index(entries, Path(spec["out"]).absolute())


if __name__ == "__main__":
    main()
