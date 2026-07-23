"""Update pinned development-tool metadata."""

import argparse
import json
import os
import re
import urllib.error
import urllib.request
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


def _asset_digest(asset: dict[str, Any], name: str) -> str:
    digest = _string(asset.get("digest"), f"digest for release asset {name}")
    algorithm, separator, value = digest.partition(":")
    if algorithm != "sha256" or not separator or _SHA256.fullmatch(value) is None:
        raise ValueError(f"release asset {name} has invalid digest {digest!r}")
    return value


def _bump_buck2(buck2: dict[str, Any]) -> None:
    repository = _string(buck2.get("repository"), "buck2.repository")
    platforms = _object(buck2.get("platforms"), "buck2.platforms")
    previous = _string(buck2.get("release"), "buck2.release")
    release = _latest_release(repository)
    tag = _string(release.get("tag_name"), "latest Buck2 release tag")
    assets = {}
    for index, raw_asset in enumerate(_array(release.get("assets"), f"assets for Buck2 {tag}")):
        asset = _object(raw_asset, f"Buck2 {tag} asset {index}")
        assets[_string(asset.get("name"), f"name of Buck2 {tag} asset {index}")] = asset
    changed = tag != previous
    for platform, raw_entry in platforms.items():
        entry = _object(raw_entry, f"buck2 platform {platform}")
        artifact = _string(entry.get("artifact"), f"artifact for buck2 platform {platform}")
        if artifact not in assets:
            raise ValueError(f"Buck2 {tag} has no {artifact} asset")
        digest = _asset_digest(assets[artifact], artifact)
        changed = changed or entry.get("sha256") != digest
        entry["sha256"] = digest
    buck2["release"] = tag
    print(f"buck2: updated {previous} -> {tag}" if changed else f"buck2: {tag} is up to date")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).with_name("tools.json"))
    parser.add_argument("--buck2", action="store_true", help="update the Buck2 fork release")
    args = parser.parse_args()
    if not args.buck2:
        parser.error("select at least one component to bump")
    return args


def main() -> None:
    args = _parse_args()
    path = args.data
    try:
        original = path.read_text(encoding="utf-8")
        data = _object(json.loads(original), str(path))
        if args.buck2:
            _bump_buck2(_object(data.get("buck2"), f"buck2 in {path}"))
        content = json.dumps(data, indent=2) + "\n"
        if content != original:
            atomic_write_text(path, content)
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        raise SystemExit(f"bump: {error}") from error


if __name__ == "__main__":
    main()
