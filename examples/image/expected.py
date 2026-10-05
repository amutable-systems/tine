#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Accept the new digests of a failing reproducibility-test.

For updating legitimate changes to the build result. Merge actual.<arch>.json into expected.json and
keep the digests of other architectures. Use `--amend` to fold them into the commit that moved them.
"""

import argparse
import json
import sys

from util import amend_paths, atomic_write_text, fail, nested_buck, package_directory

PACKAGE = "tine//examples/image"
EXPECTATIONS = "expected.json"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="expected", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--amend", action="store_true", help="fold the refreshed digests into the commit at HEAD"
    )
    args = parser.parse_args(argv)

    directory = package_directory(nested_buck(), PACKAGE)
    path = directory / EXPECTATIONS
    actual = sorted(directory.glob("actual.*.json"))
    if not actual:
        fail(f"expected: no actual.*.json beside {path}; a failing reproducibility-test writes them")

    recorded = json.loads(path.read_text(encoding="utf-8"))
    for source in actual:
        for architecture, digests in json.loads(source.read_text(encoding="utf-8")).items():
            if architecture not in recorded:
                fail(f"expected: {source} has {architecture}, which {path} does not track")
            print(f"==> {architecture}, from {source}", file=sys.stderr)
            for name, entry in sorted(digests.items()):
                was = recorded[architecture].get(name, {}).get("sha256")
                now = entry["sha256"]
                print(f"    {name}: {'unchanged' if now == was else f'{was} -> {now}'}", file=sys.stderr)
            recorded[architecture] = digests
    atomic_write_text(path, json.dumps(recorded, indent=2, sort_keys=True) + "\n")

    if args.amend and not amend_paths(directory, EXPECTATIONS):
        print("==> every digest is already recorded, nothing to amend", file=sys.stderr)


if __name__ == "__main__":
    main()
