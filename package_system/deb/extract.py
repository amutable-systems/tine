#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Bootstrap a box by unpacking Debian packages without dpkg.

A deb's data member is an ordinary compressed tar of the tree it installs, so the bootstrap needs
no package tooling. The control member, the maintainer scripts in it, and the package database
they belong to are all deferred to the real install that follows.
"""

import debfile

import extractor


def main(argv: list[str] | None = None) -> None:
    extractor.run("extract", debfile.unpack, argv)


if __name__ == "__main__":
    main()
