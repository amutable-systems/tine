#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Snapshot the archives of the Go releases that a go.releases target declares.

refresh-catalog runs the snapshot through the target's `[snapshot]` sub-target and writes the output
into the catalog. With `--advance`, the snapshot contains the newest patch release of every minor
version from Go 1.21 on instead.
"""

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, TypedDict, cast

import goversion
import specs
from util import fail, text_destination, urlopen, with_retries

# go.dev's list of every Go release contains the sha256 and size of every archive, so a snapshot
# downloads no archive.
INDEX = "https://go.dev/dl/?mode=json&include=all"

_SHA256 = re.compile(r"[0-9a-f]{64}")
# go.dev names the first release of every minor version from Go 1.21 on with a .0 patch, such as
# go1.21.0. An advance therefore starts at Go 1.21.
_FIRST_MINOR = (1, 21)


class Spec(TypedDict):
    # The committed snapshot.
    current: str
    # Go's name for each architecture, keyed by tine's name for it.
    goarch: dict[str, str]


def _object(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"go-releases: {what} is not a JSON object")
    return cast(dict[str, Any], value)


def _list(value: object, what: str) -> list[Any]:
    if not isinstance(value, list):
        fail(f"go-releases: {what} is not a JSON array")
    return cast(list[Any], value)


def advance(current: list[str], index: list[Any]) -> list[str]:
    """Return the newest patch release of every minor version from Go 1.21 on.

    A release in `current` that is newer than every release that `index` lists for its minor version
    stays, so an advance never moves a release backwards.
    """
    newest: dict[tuple[int, int], tuple[int, str]] = {}
    for version in [*current, *(release.get("version") for release in index if isinstance(release, dict))]:
        parsed = goversion.patch_release(version)
        if parsed is None:
            continue
        major, minor, patch = parsed
        if (major, minor) >= _FIRST_MINOR and (
            (major, minor) not in newest or patch > newest[major, minor][0]
        ):
            newest[major, minor] = (patch, f"go{major}.{minor}.{patch}")
    return sorted(release for _, release in newest.values())


def snapshot(
    goarch: dict[str, str], releases: list[str], index: list[Any]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return the URL, sha256 and size of each release's archive, per architecture."""
    listed = {}
    for raw in index:
        release = _object(raw, f"a release in {INDEX}")
        listed[release.get("version")] = release
    result = {}
    for version in sorted(releases):
        release = listed.get(version)
        if release is None:
            fail(f"go-releases: {INDEX} does not list {version}")
        files = {}
        for raw in _list(release.get("files"), f"the files of {version}"):
            file = _object(raw, f"a file of {version}")
            files[file.get("filename")] = file
        archives = {}
        for arch, name in sorted(goarch.items()):
            filename = f"{version}.linux-{name}.tar.gz"
            file = files.get(filename)
            if file is None:
                fail(f"go-releases: {INDEX} lists no archive {filename}")
            digest = file.get("sha256")
            size = file.get("size")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                fail(f"go-releases: {INDEX} lists {digest!r} as the sha256 of {filename}")
            # Python counts true and false as int. A size of true in the index is malformed, so the
            # check rejects bool explicitly.
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                fail(f"go-releases: {INDEX} lists {size!r} as the size of {filename}")
            archives[arch] = {
                "sha256": digest,
                "size": size,
                "url": urllib.parse.urljoin(INDEX, filename),
            }
        result[version] = archives
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="go-releases")
    specs.add_argument(parser)
    parser.add_argument("--out", type=Path, required=True, help="snapshot path to write, or `-` for stdout")
    parser.add_argument(
        "--advance",
        action="store_true",
        help="snapshot the newest patch release of every minor version from Go 1.21 on",
    )
    args = parser.parse_args(argv)
    spec = specs.load(Spec, args.spec, prog="go-releases")

    def fetch() -> object:
        with urlopen(INDEX, agent="tine-snapshot") as response:
            return json.load(response)

    index = _list(with_retries(f"go-releases: {INDEX}", fetch), INDEX)
    current = sorted(_object(json.loads(Path(spec["current"]).read_text(encoding="utf-8")), spec["current"]))
    releases = advance(current, index) if args.advance else current
    committed = {}
    for release in current:
        parsed = goversion.patch_release(release)
        if parsed is not None:
            committed[parsed[:2]] = release
    for release in sorted(set(releases) - set(current)):
        parsed = goversion.patch_release(release)
        previous = None if parsed is None else committed.get(parsed[:2])
        if previous is None:
            print(f"==> go-releases: adding {release}", file=sys.stderr)
        else:
            print(f"==> go-releases: advancing {previous} to {release}", file=sys.stderr)
    with text_destination(args.out) as output:
        output.write(json.dumps(snapshot(spec["goarch"], releases, index), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
