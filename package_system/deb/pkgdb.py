#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Copy the dpkg database out of a logical image as a separate, trimmed artifact.

Useful as a basis for SBOM creation and security scanners. The database is not shipped in the
image, so capture it as a separate artifact. dpkg's is a `status` file describing every installed
package and a directory of per-package files beside it, so the output is a reproducible archive of
the two.

Of those per-package files only the `.list` is kept, which is what a package owns. The maintainer
scripts are the package's code rather than a description of it, and `.md5sums` is a manifest dpkg
alone reads to verify an installed tree, which is the largest part of the database after the file
lists.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import debfile
import specs
import util

import finalize
import tar


class Spec(finalize.ImageSpec):
    out: str


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "pkgdb", argv)

    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    out = Path(spec["out"])
    with finalize.image(spec, program="pkgdb") as tree:
        source = tree / debfile.ADMINDIR
        status = source / "status"
        if not status.is_file():
            util.fail(f"no dpkg database at {source}; the image has no installed packages")
        listings = sorted((source / "info").glob("*.list"))
        if not listings:
            util.fail(f"no dpkg file lists under {source}")
        # Beside the output rather than in TMPDIR, so reflink cloning keeps working on the way
        # through and Buck only ever sees the finished file.
        with tempfile.TemporaryDirectory(dir=out.parent) as scratch:
            trimmed = Path(scratch) / "dpkg"
            (trimmed / "info").mkdir(parents=True)
            shutil.copy2(status, trimmed / "status")
            for listing in listings:
                shutil.copy2(listing, trimmed / "info" / listing.name)
            packed = Path(scratch) / out.name
            tar.pack_tree(trimmed, packed, epoch)
            util.compress_zstd(packed, out)
    print(f"pkgdb: captured {len(listings)} package entries -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
