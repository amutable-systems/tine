"""Update pinned development-tool metadata."""

import argparse
import functools
import json
import os
import re
import subprocess
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from util import atomic_write_text

_SHA256 = re.compile(r"[0-9a-f]{64}")


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{description} must be a non-empty string")
    return value


def _object(value: object, description: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{description} must be an object")
    return cast(dict[str, Any], value)


def _integer(value: object, description: str) -> int:
    # bool is an int in python, and a JSON true here would be a corrupt pin rather than a size.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{description} must be a non-negative integer")
    return value


def _array(value: object, description: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{description} must be an array")
    return cast(list[object], value)


def _github_json(url: str) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "tine-bump",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _latest_release(repository: str) -> dict[str, Any]:
    releases = _array(
        _github_json(f"https://api.github.com/repos/{repository}/releases?per_page=100"),
        f"GitHub releases for {repository}",
    )
    published = []
    for index, raw_release in enumerate(releases):
        release = _object(raw_release, f"GitHub release {index}")
        if release.get("draft") is False and isinstance(release.get("published_at"), str):
            published.append(release)
    if not published:
        raise ValueError(f"GitHub reports no published releases for {repository}")
    return max(published, key=lambda release: release["published_at"])


def _latest_full_release(repository: str) -> dict[str, Any]:
    """Return the repository's latest full (non-prerelease, non-draft) release.

    `/releases/latest` is both lighter than scanning every release and the upstream's own idea of
    "stable". Repositories that publish only prereleases have no such release, so fall back to the scan.
    """
    try:
        return _object(
            _github_json(f"https://api.github.com/repos/{repository}/releases/latest"),
            f"latest release for {repository}",
        )
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return _latest_release(repository)


def _release_assets(release: dict[str, Any], tag: str) -> list[dict[str, Any]]:
    return [
        _object(asset, f"asset {index} of {tag}")
        for index, asset in enumerate(_array(release.get("assets"), f"assets for {tag}"))
    ]


def _asset_digest(asset: dict[str, Any], name: str) -> str:
    digest = _string(asset.get("digest"), f"digest for release asset {name}")
    algorithm, separator, value = digest.partition(":")
    if algorithm != "sha256" or not separator or _SHA256.fullmatch(value) is None:
        raise ValueError(f"release asset {name} has invalid digest {digest!r}")
    return value


def _python_minor(path: Path) -> str:
    """The pinned CPython minor (e.g. "3.14"), read only for a CPython pin.

    Lazily, because a project pinning no CPython has no reason to carry the configuration this
    reads: every other artifact matches its successor without it.
    """
    data = _object(tomllib.loads(path.read_text(encoding="utf-8")), str(path))
    tool = _object(data.get("tool"), f"tool table in {path}")
    ty = _object(tool.get("ty"), f"tool.ty in {path}")
    environment = _object(ty.get("environment"), f"tool.ty.environment in {path}")
    return _string(environment.get("python-version"), f"tool.ty.environment.python-version in {path}")


def _asset_regex(artifact: str, tag: str, python_minor: Callable[[], str]) -> re.Pattern[str]:
    """Match the successor of `artifact` across releases by wildcarding only its version parts.

    python-build-standalone artifacts carry both a CPython version and a date, so pin the minor (from
    pyproject) and the platform/variant while letting the patch and date float. Every other upstream
    embeds at most the release tag, so wildcard the tag (with and without a leading "v").
    """
    if artifact.startswith("cpython-"):
        match = re.match(r"cpython-\d+\.\d+\.\d+\+\d+-(.+)$", artifact)
        if match is None:
            raise ValueError(f"cannot parse CPython artifact {artifact!r}")
        return re.compile(rf"cpython-{re.escape(python_minor())}\.\d+\+\d+-{re.escape(match.group(1))}")
    pattern = re.escape(artifact)
    for token in {tag, tag.lstrip("v")}:
        if token:
            pattern = pattern.replace(re.escape(token), r"[^/]+")
    return re.compile(pattern)


def _select_asset(assets: list[dict[str, Any]], description: str) -> dict[str, Any]:
    if not assets:
        raise ValueError(f"no release asset matches {description}")
    if len(assets) == 1:
        return assets[0]
    # Several patch builds can match (e.g. two CPython 3.14.x in one release); take the newest.
    return max(
        assets,
        key=lambda asset: [
            int(number) for number in re.findall(r"\d+", _string(asset.get("name"), "asset name"))
        ],
    )


def _bump_tool(
    name: str, spec: dict[str, Any], python_minor: Callable[[], str], releases: dict[str, dict[str, Any]]
) -> tuple[str, str] | None:
    """Resolve the latest release for one tool and rewrite its per-platform pins.

    Returns the (old, new) release tags when anything changed, else None.

    Projects which only publish prereleases resolve through _latest_full_release's scan fallback;
    every other tool has a plain latest release. Version-independent artifact names match verbatim;
    those embedding a version (syft, python-build-standalone) match by _asset_regex.
    """
    repository = _string(spec.get("repository"), f"{name}.repository")
    platforms = _object(spec.get("platforms"), f"{name}.platforms")
    previous = _string(spec.get("release"), f"{name}.release")
    release = releases.get(repository)
    if release is None:
        release = _latest_full_release(repository)
        releases[repository] = release
    tag = _string(release.get("tag_name"), f"tag for {repository} latest release")
    assets = _release_assets(release, tag)
    changed = tag != previous
    for arch, raw_entry in platforms.items():
        entry = _object(raw_entry, f"{name} platform {arch}")
        artifact = _string(entry.get("artifact"), f"artifact for {name} platform {arch}")
        pattern = _asset_regex(artifact, previous, python_minor)
        matched = [asset for asset in assets if pattern.fullmatch(_string(asset.get("name"), "asset name"))]
        asset = _select_asset(matched, f"/{pattern.pattern}/ in {repository} {tag}")
        new_artifact = _string(asset.get("name"), "asset name")
        digest = _asset_digest(asset, new_artifact)
        # Pinned so that buck skips the HEAD request it would otherwise size the download with.
        size = _integer(asset.get("size"), f"size for release asset {new_artifact}")
        changed = (
            changed
            or entry.get("artifact") != new_artifact
            or entry.get("sha256") != digest
            or entry.get("size") != size
        )
        entry["artifact"] = new_artifact
        entry["sha256"] = digest
        entry["size"] = size
    spec["release"] = tag
    print(f"{name}: updated {previous} -> {tag}" if changed else f"{name}: {tag} is up to date")
    return (previous, tag) if changed else None


def _commit(path: Path, updates: list[tuple[str, str, str]]) -> None:
    """Commit the rewritten pins with a message itemizing each update.

    These are mechanical, machine-generated commits, so they are not signed off.
    """
    body = "\n".join(f"- {name}: {previous} → {new}" for name, previous, new in updates)
    message = f"tool: Bump pinned tool releases\n\n{body}\n"
    subprocess.run(
        ["git", "-C", str(path.parent), "commit", "--file=-", "--", path.name],
        input=message,
        encoding="utf-8",
        check=True,
    )


def _selected_names(args: argparse.Namespace, data: dict[str, Any]) -> list[str]:
    if args.all:
        return list(data)
    for name in args.tool:
        if name not in data:
            raise ValueError(f"unknown tool {name!r}; known tools: {', '.join(sorted(data))}")
    return list(args.tool)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).with_name("tools.json"))
    parser.add_argument(
        "--tool",
        action="append",
        default=[],
        metavar="NAME",
        help="update a pinned tool by name, e.g. ruff or ty (repeatable)",
    )
    parser.add_argument("--all", action="store_true", help="update every pinned tool")
    parser.add_argument(
        "--commit", action="store_true", help="commit the updated pins with an itemized message"
    )
    args = parser.parse_args()
    if not (args.tool or args.all):
        parser.error("select at least one tool to bump")
    return args


def main() -> None:
    args = _parse_args()
    path = args.data
    try:
        original = path.read_text(encoding="utf-8")
        data = _object(json.loads(original), str(path))
        python_minor = functools.cache(lambda: _python_minor(path.parent.parent / "pyproject.toml"))
        releases: dict[str, dict[str, Any]] = {}
        updates: list[tuple[str, str, str]] = []
        for name in _selected_names(args, data):
            change = _bump_tool(name, _object(data.get(name), f"{name} in {path}"), python_minor, releases)
            if change is not None:
                updates.append((name, *change))
        content = json.dumps(data, indent=2) + "\n"
        if content != original:
            atomic_write_text(path, content)
        if args.commit and updates:
            _commit(path, updates)
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        urllib.error.URLError,
    ) as error:
        raise SystemExit(f"bump: {error}") from error


if __name__ == "__main__":
    main()
