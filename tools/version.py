#!/usr/bin/python3
"""Derive an image version from git state, to inject as build configuration.

    release   1.4.2            clean checkout of tag v1.4.2
    snapshot  1.4.2^3-08f2c4   clean checkout, 3 commits past the tag
    dev       1.4.2^3^86400    dirty tree, 86400 seconds after the last commit

"Image versioning" in images.md explains the shapes, their ordering, and the
partition label length budget.
"""

import argparse
import re
import subprocess
import time
from pathlib import Path

GPT_LABEL_LIMIT = 36

# The character set accepted in partition labels and UKI file names; "^" is excluded
# so that the separators appended below stay unambiguous.
TAG_RE = re.compile(r"^[a-zA-Z0-9._~-]+$")


def _git(*args: str, directory: Path) -> str:
    """Run git, letting stderr through for diagnosis; no invocation is expected to fail."""
    return subprocess.run(
        ["git", "-C", str(directory), *args], text=True, stdout=subprocess.PIPE, check=True
    ).stdout.strip()


def _fits(version: str, template: str) -> bool:
    return len(template.format(version=version)) <= GPT_LABEL_LIMIT


def compute_version(directory: Path, template: str) -> str:
    """Compute the version of the given repository's HEAD."""
    head = _git("rev-parse", "HEAD", directory=directory)
    # --always falls back to the bare commit hash when no v-tag is reachable; use the 0.0.0 base then.
    described = _git("describe", "--tags", "--match", "v*", "--abbrev=0", "--always", directory=directory)
    if described.startswith("v"):
        base = described.removeprefix("v")
        count = int(_git("rev-list", "--count", f"{described}..HEAD", directory=directory))
    else:
        base = "0.0.0"
        count = int(_git("rev-list", "--count", "HEAD", directory=directory))
    if not TAG_RE.match(base):
        raise SystemExit(f"version: tag {described!r} is not a valid version")

    if _git("status", "--porcelain", directory=directory):
        committed = int(_git("log", "-1", "--format=%ct", directory=directory))
        now = int(time.time())
        # A clock behind the committer time must not order the build below the commit.
        version = f"{base}^{count}^{max(0, now - committed)}"
        if not _fits(version, template):
            raise SystemExit(
                f"version: {version!r} does not fit {template!r} in {GPT_LABEL_LIMIT} characters"
            )
        return version

    if count == 0:
        if not _fits(base, template):
            raise SystemExit(f"version: {base!r} does not fit {template!r} in {GPT_LABEL_LIMIT} characters")
        return base

    # git only lengthens the hash abbreviation on ambiguity, so shrink manually: the hash names
    # the commit for tracking, ordering comes from the tag and the commit count.
    for hashlen in range(12, 3, -1):
        version = f"{base}^{count}-{head[:hashlen]}"
        if _fits(version, template):
            return version
    raise SystemExit(
        f"version: {base}^{count}-<hash> does not fit {template!r} in {GPT_LABEL_LIMIT} characters"
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="version", description=__doc__)
    p.add_argument(
        "--directory", type=Path, default=Path(), help="the git repository to describe (default: .)"
    )
    p.add_argument(
        "label_template",
        metavar="LABEL_TEMPLATE",
        help="the longest partition label pattern, with a {version} placeholder, whose rendering "
        "must fit a GPT label, e.g. 'myos_{version}'; images without versioned "
        "labels (version only in os-release and the UKI name) pass plain '{version}'",
    )
    args = p.parse_args(argv)
    if "{version}" not in args.label_template:
        p.error("LABEL_TEMPLATE must contain a {version} placeholder")
    print(compute_version(args.directory, args.label_template))


if __name__ == "__main__":
    main()
