#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Bootstrap a box by unpacking Debian packages without dpkg.

The data member of a deb is a compressed tar of the files that the package installs, so Python
alone can unpack it. This driver does not run maintainer scripts and does not write the dpkg
database. The install that follows the bootstrap runs the scripts and writes the database.
"""

import debfile

import extractor


def main(argv: list[str] | None = None) -> None:
    extractor.run("extract", debfile.unpack, argv)


if __name__ == "__main__":
    main()
