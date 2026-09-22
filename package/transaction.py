# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The transaction a planner writes and a package closure is selected from.

One schema, shared by every package system's planner, because the same Starlark reads all of
them: `select_package_artifacts` looks each entry up in its repository's pool by checksum, and a
committed box lock is one of these files kept verbatim. What differs between package systems
is how the closure is arrived at, not how it is described.
"""

import json
from pathlib import Path
from typing import Literal, NamedTuple, NotRequired, TypedDict

from util import fail, text_destination


class Repository(NamedTuple):
    id: str
    path: Path
    priority: int
    baseurl: str | None


class RepositorySpec(TypedDict):
    id: str
    directory: str
    priority: int
    baseurl: str | None


class Spec(TypedDict):
    arch: str
    repositories: list[RepositorySpec]


class TransactionPackage(TypedDict):
    package_id: str
    repo: str
    pkg_checksum: str
    source: Literal["local", "repo"]
    # A local package names an input directory and file; a remote one names its transport.
    location: NotRequired[str]
    size: NotRequired[int]
    url: NotRequired[str]


def load_repositories(spec: Spec, *, absolute: bool = False) -> list[Repository]:
    """Read the configured repositories a solve runs against, in declaration order."""
    return [
        Repository(
            repository["id"],
            Path(repository["directory"]).absolute() if absolute else Path(repository["directory"]),
            repository["priority"],
            repository["baseurl"],
        )
        for repository in spec["repositories"]
    ]


def checksum(what: str, value: str) -> str:
    """Check that a package's digest is one a pool can be keyed by."""
    digest = value.lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        fail(f"{what} has invalid sha256 {value!r}")
    return digest


def entry(
    package_id: str,
    repository: Repository,
    digest: str,
    location: str,
    size: int = 0,
) -> TransactionPackage:
    """Describe one package the closure must materialize.

    A repository with no base URL is one this build produced, so its packages are projected from
    the directory that produced them rather than fetched.
    """
    package = TransactionPackage(
        package_id=package_id,
        repo=repository.id,
        pkg_checksum=checksum(package_id, digest),
        source="local" if repository.baseurl is None else "repo",
    )
    if repository.baseurl is None:
        package["location"] = location
    else:
        if size <= 0:
            fail(f"{package_id} has invalid download size {size}")
        package["size"] = size
        package["url"] = repository.baseurl.rstrip("/") + "/" + location.lstrip("/")
    return package


def write(path: Path, packages: list[TransactionPackage]) -> None:
    """Order a transaction before writing it, so identical solves give identical bytes."""
    packages.sort(
        key=lambda package: (
            package["repo"],
            package["package_id"],
            package["pkg_checksum"],
            package.get("location", ""),
            package.get("url", ""),
        )
    )
    with text_destination(path) as output:
        json.dump(packages, output, indent=2)
        output.write("\n")
