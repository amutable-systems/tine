#!/usr/bin/python3
"""Record what the tracked targets currently build, in ./expected.json.

For updating legitimate changes to to the build result. Does not currently commit the change,
amend it to the causing commit.
"""

import hashlib
import json
import sys
from pathlib import Path
from typing import cast

from util import atomic_write_text, buck_output, nested_buck, package_directory

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
            raise SystemExit(f"expected: {target} builds no single artifact to digest")
        with Path(path).open("rb") as stream:
            digests[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return digests


def main() -> None:
    buck = nested_buck()
    path = package_directory(buck, PACKAGE) / EXPECTATIONS
    expectations = json.loads(path.read_text(encoding="utf-8"))
    if not expectations:
        raise SystemExit(f"expected: {path} tracks no target")

    names = sorted(expectations)
    print(f"==> building {len(names)} tracked target(s)", file=sys.stderr)
    for name, digest in _digests(buck, names).items():
        was = expectations[name].get("sha256")
        print(f"    {name}: {'unchanged' if digest == was else f'{was} -> {digest}'}", file=sys.stderr)
        expectations[name]["sha256"] = digest
    atomic_write_text(path, json.dumps(expectations, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
