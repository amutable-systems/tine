#!/usr/bin/python3
"""Write the listing of a release, which says what each published file is.

`artifacts_<arch>.json` gives every published file its kind, its image, and for a DDI its verity root
hash, so whatever authors or consumes the release reads roles rather than names.
"""

import json
import re
from pathlib import Path
from typing import Any, TypedDict

import specs


class Entry(TypedDict):
    image: str
    kind: str
    path: str


class Spec(TypedDict):
    arch: str
    files: dict[str, Entry]
    out: str
    # This maps each image to its disk's usr verity root hash file.
    root_hashes: dict[str, str]


def read_hash(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SystemExit(f"publish: {path} holds no verity root hash: {value!r}")
    return value


def _of_image(files: dict[str, Entry], image: str, kind: str) -> list[str]:
    return sorted(name for name, entry in files.items() if entry["image"] == image and entry["kind"] == kind)


def root_hash(name: str, files: dict[str, Entry], root_hashes: dict[str, str]) -> str | None:
    """Return the verity root hash of an extension or a usr partition.

    An extension's hash comes from its roothash file, and a usr partition's hash is its disk's.
    The result is None for any other file and for a DDI without verity.
    """
    entry = files[name]
    if entry["kind"] == "sysext":
        sidecars = _of_image(files, entry["image"], "roothash")
        if len(sidecars) > 1:
            raise SystemExit(f"publish: {name} has {len(sidecars)} root hash files, expected one")
        return read_hash(Path(files[sidecars[0]]["path"])) if sidecars else None
    if entry["kind"] == "usr":
        path = root_hashes.get(entry["image"])
        if path is None:
            return None
        partitions = _of_image(files, entry["image"], "usr")
        if len(partitions) != 1:
            raise SystemExit(
                f"publish: {len(partitions)} usr partitions in {entry['image']} share one root hash"
            )
        return read_hash(Path(path))
    return None


def listing(spec: Spec) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for name in sorted(spec["files"]):
        entry = spec["files"][name]
        described: dict[str, Any] = {"image": entry["image"], "kind": entry["kind"]}
        digest = root_hash(name, spec["files"], spec["root_hashes"])
        if digest is not None:
            described["root_hash"] = digest
        files[name] = described
    return {"arch": spec["arch"], "files": files}


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "publish", argv)
    Path(spec["out"]).write_text(
        json.dumps(listing(spec), indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
