#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Record what the tracked targets currently build, in ./expected.json.

For updating legitimate changes to the build result. `--amend` folds the new digests into the
commit that moved them, which is where they belong; without it, amend them by hand.

A single run records the sum for the current host architecture; the others are kept.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import cast

from util import amend_paths, atomic_write_text, buck_output, fail, nested_buck, package_directory

PACKAGE = "tine//examples/image"
EXPECTATIONS = "expected.json"


def _digests(buck: str, names: list[str]) -> dict[str, str]:
    targets = [f"{PACKAGE}:{name}" for name in names]
    output = buck_output(buck, "build", "--show-full-json-output", *targets)
    built = cast(dict[str, str], json.loads(output))
    digests = {}
    for name, target in zip(names, targets, strict=True):
        path = built.get(target)
        if not path:
            fail(f"expected: {target} builds no single artifact to digest")
        with Path(path).open("rb") as stream:
            digests[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return digests


def _host_architecture(buck: str) -> str:
    output = buck_output(
        buck, "uquery", "--json", "--output-attribute=^cpu_configuration$", "tine//platforms:default"
    )
    attributes = cast(dict[str, dict[str, str]], json.loads(output))
    return attributes["tine//platforms:default"]["cpu_configuration"].rsplit(":", 1)[1]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="expected", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--amend", action="store_true", help="fold the refreshed digests into the commit at HEAD"
    )
    args = parser.parse_args(argv)

    buck = nested_buck()
    path = package_directory(buck, PACKAGE) / EXPECTATIONS
    recorded = json.loads(path.read_text(encoding="utf-8"))
    architecture = _host_architecture(buck)
    expectations = recorded.get(architecture)
    if not expectations:
        fail(f"expected: {path} tracks no target for {architecture}, only {sorted(recorded)}")

    names = sorted(expectations)
    print(f"==> building {len(names)} tracked target(s) for {architecture}", file=sys.stderr)
    for name, digest in _digests(buck, names).items():
        was = expectations[name].get("sha256")
        print(f"    {name}: {'unchanged' if digest == was else f'{was} -> {digest}'}", file=sys.stderr)
        expectations[name]["sha256"] = digest
    atomic_write_text(path, json.dumps(recorded, indent=2, sort_keys=True) + "\n")

    if args.amend and not amend_paths(path.parent, EXPECTATIONS):
        print("==> every digest is already recorded, nothing to amend", file=sys.stderr)


if __name__ == "__main__":
    main()
