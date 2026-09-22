#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Expand the placeholders in a file a project ships as a template.

A project that expects its build system to fill in a prefix or a port commits the file with markers
and a `sed` in its install recipe. Running that here instead keeps the values in the declaration
that installs the file, and hands the image an ordinary artifact.
"""

import shutil
from pathlib import Path
from typing import TypedDict

import specs
from util import fail


class Spec(TypedDict):
    # The target, named in whatever this refuses.
    name: str
    # Where to write the expanded file.
    out: str
    # Placeholder -> what to put in its place.
    replacements: dict[str, str]
    # The template.
    src: str


def expand(target: str, template: str, replacements: dict[str, str]) -> str:
    """`template` with every replacement applied, each of which has to match something.

    A placeholder that matches nothing is a failure rather than a no-op: it means the project renamed
    it, and the file would otherwise be installed with a marker left in it.
    """
    expanded = template
    for placeholder, value in sorted(replacements.items()):
        if placeholder not in expanded:
            fail(f"substitute {target}: the template holds no {placeholder}")
        expanded = expanded.replace(placeholder, value)
    return expanded


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "substitute", argv)
    src, out = Path(spec["src"]), Path(spec["out"])
    template = src.read_text(encoding="utf-8")
    out.write_text(expand(spec["name"], template, spec["replacements"]), encoding="utf-8")

    # The template's own mode, so that a substituted script stays executable.
    shutil.copymode(src, out)


if __name__ == "__main__":
    main()
