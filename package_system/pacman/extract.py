# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Bootstrap a box by unpacking alpm packages without pacman.

An alpm package is an ordinary compressed tar of the tree it installs, so the bootstrap needs
no package tooling and no separate payload representation. Its metadata and install scriptlets
are deferred to the real install that follows.
"""

from contextlib import ExitStack
from pathlib import Path

import alpm
import extractor


def extract(package: Path, dest: Path) -> int:
    """Extract one package's tree into dest and return the number of entries written."""
    written = 0
    with ExitStack() as stack:
        archive = alpm.open_package(stack, package)
        for member in archive:
            # alpm reserves every top-level dot entry for its own metadata, so none are files
            # the package installs. Their contents belong to the real install that follows.
            if member.name.startswith("."):
                continue
            archive.extract(member, dest, filter="tar")
            written += 1
    return written


def main(argv: list[str] | None = None) -> None:
    extractor.run("extract", extract, argv)


if __name__ == "__main__":
    main()
