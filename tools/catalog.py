"""Refresh pure catalog snapshots, then resolve engine transactions against those pins.

Repositories pinned to an rpmrepo mirror first advance their declared snapshot to the newest
one the gateway enumerates by rewriting their declaration; roll back by hand-editing the pin.

Remote engine-lock entries retain their package transports, so a repository's package pool keeps
the committed engine available after its repodata advances.

The host orchestrator discovers refresh subtargets and appends only output paths.
Nested Buck reuses the invoking daemon through the inherited isolation directory.
"""

import argparse
import contextlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _buck_out(buck: str, *args: str) -> str:
    return subprocess.run([buck, *args], check=True, capture_output=True, text=True).stdout.strip()


def _refresh_targets(buck: str, kind: str) -> list[str]:
    """The refresh bindings of rule `kind` in the active catalog package."""
    return sorted(_buck_out(buck, "uquery", f"kind('^{kind}$', catalog//:)").split())


def _name_of(target: str) -> str:
    return target.rsplit(":", 1)[1]


def _snapshot_path(catalog_dir: Path, target: str, kind: str, suffix: str) -> Path:
    name = _name_of(target)
    if not name.endswith(suffix):
        raise SystemExit(f"catalog: {target} does not end with {suffix!r}")
    return catalog_dir / "snapshot" / kind / f"{name.removesuffix(suffix)}.json"


def _engine_snapshot_path(catalog_dir: Path, target: str) -> Path:
    return _snapshot_path(catalog_dir, target, "engine", ".engine")


def _repository_snapshot_path(catalog_dir: Path, target: str) -> Path:
    return _snapshot_path(catalog_dir, target, "repo", ".repository")


def _run(buck: str, target: str, args: list[str]) -> None:
    # Mute nested Buck while preserving driver progress on stderr.
    subprocess.run([buck, "-v", "0", "run", target, "--console", "none", "--", *args], check=True)


def _rpmrepo_repositories(buck: str) -> dict[str, tuple[str, str]]:
    """Map repository targets carrying an rpmrepo pin in their metadata to (mirror, snapshot)."""
    out = _buck_out(
        buck,
        "uquery",
        "--json",
        "--output-attribute=^metadata$",
        "kind('^_remote_repository$', catalog//:)",
    )
    repositories = {}
    for target, attributes in json.loads(out).items():
        metadata = attributes.get("metadata") or {}
        mirror = metadata.get("rpmrepo.mirror")
        snapshot = metadata.get("rpmrepo.snapshot")
        if mirror is not None and snapshot is not None:
            repositories[target] = (mirror, snapshot)
    return repositories


def _series(snapshot: str) -> str:
    """A snapshot id's series: everything before the trailing datestamp (rpmrepo's naming)."""
    return snapshot.rsplit("-", 1)[0]


def _newest_snapshot(repository: str, mirror: str, series: str) -> str:
    """The newest snapshot of `series` that the mirror's gateway enumerates."""
    gateway, found, _ = mirror.partition("/v2/mirror/")
    if not found:
        raise SystemExit(f"{repository}: mirror {mirror!r} is not an rpmrepo /v2/mirror/ URL")
    # CDN bot filters (e.g. Cloudflare's) reject Python's default agent.
    request = urllib.request.Request(gateway + "/v2/enumerate", headers={"User-Agent": "tine-catalog"})

    def enumerate_snapshots() -> list[object]:
        for attempt in range(1, 5):
            try:
                with urllib.request.urlopen(request) as response:
                    return json.load(response)
            except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
                # Retry transient failures; unstable connections reset mid-handshake.
                transient = getattr(error, "code", None) in (None, 408, 429, 500, 502, 503, 504)
                if not transient or attempt == 4:
                    raise
                print(f"{repository}: enumerate: {error}; retrying…", file=sys.stderr)
                time.sleep(2**attempt)
        raise AssertionError("unreachable")

    snapshots = enumerate_snapshots()
    matches = [s for s in snapshots if isinstance(s, str) and _series(s) == series]
    if not matches:
        raise SystemExit(f"{repository}: the mirror enumerates no {series!r} snapshots")
    # Snapshot ids end in a datestamp, so the newest sorts last.
    return max(matches)


def _advance_snapshots(buck: str, catalog_dir: Path) -> None:
    """Advance rpmrepo-mirrored repositories to the newest snapshot their gateway enumerates."""
    for target, (mirror, current) in sorted(_rpmrepo_repositories(buck).items()):
        repository = _name_of(target)
        wanted = _newest_snapshot(repository, mirror, _series(current))
        if wanted == current:
            continue
        package = target.split("//", 1)[1].rsplit(":", 1)[0]
        declaration = catalog_dir / package / "BUCK" if package else catalog_dir / "BUCK"
        pin = f'rpmrepo_snapshot = "{current}"'
        content = declaration.read_text(encoding="utf-8")
        if content.count(pin) != 1:
            raise SystemExit(f"{repository}: expected exactly one {pin!r} in {declaration}")
        print(f"==> advancing {repository} to {wanted} (from {current})", file=sys.stderr)
        declaration.write_text(content.replace(pin, f'rpmrepo_snapshot = "{wanted}"'), encoding="utf-8")


def _snapshot(buck: str, target: str, catalog_dir: Path) -> None:
    """Atomically replace a repository snapshot with its current pure metadata."""
    repository = _name_of(target)
    print(f"==> snapshotting {repository} (via {target}[snapshot])", file=sys.stderr)
    committed = _repository_snapshot_path(catalog_dir, target)
    committed.parent.mkdir(parents=True, exist_ok=True)
    _run(buck, f"{target}[snapshot]", ["--out", str(committed)])


def _resolve(buck: str, target: str, catalog_dir: Path) -> None:
    engine = _name_of(target)
    print(f"==> resolving {engine} (via {target}[resolve])", file=sys.stderr)
    committed = _engine_snapshot_path(catalog_dir, target)
    committed.parent.mkdir(parents=True, exist_ok=True)
    _run(buck, f"{target}[resolve]", ["--out", str(committed)])


def _select_engines(all_resolves: list[str], selected_engines: list[str] | None) -> list[str]:
    if selected_engines is None:
        return all_resolves
    duplicates = sorted({name for name in selected_engines if selected_engines.count(name) > 1})
    if duplicates:
        raise SystemExit(f"catalog: engine names selected more than once: {duplicates}")
    by_name = {_name_of(target): target for target in all_resolves}
    unknown = sorted(set(selected_engines) - by_name.keys())
    if unknown:
        raise SystemExit(f"catalog: unknown engine names: {unknown}")
    selected = set(selected_engines)
    return [target for target in all_resolves if _name_of(target) in selected]


def _refresh(
    buck: str,
    catalog_dir: Path,
    selected_engines: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Snapshot repositories and resolve selected engines."""
    all_resolves = _refresh_targets(buck, "_engine")
    resolves = _select_engines(all_resolves, selected_engines)

    snapshots = _refresh_targets(buck, "_remote_repository")
    for target in snapshots:
        _snapshot(buck, target, catalog_dir)

    for target in resolves:
        _resolve(buck, target, catalog_dir)

    return snapshots, resolves


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="catalog")
    p.add_argument(
        "--buck", default="buck", help="buck binary to nest (aliases pass the pinned one; default: PATH)"
    )
    p.add_argument(
        "--engine",
        action="append",
        help="only (re)resolve these engines (repos still all snapshot); default: all",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="assert the committed catalog matches what the pinned resolvers produce (CI)",
    )
    args = p.parse_args(argv)
    catalog_dir = Path(_buck_out(args.buck, "audit", "cell", "catalog", "--paths-only"))

    # Run nested commands from the project root so wrappers resolve consistently.
    with contextlib.chdir(_buck_out(args.buck, "root", "--kind", "project")):
        # Verify checks the committed pins as-is; refresh first advances rpmrepo pins.
        if not args.verify:
            _advance_snapshots(args.buck, catalog_dir)

        snapshots, resolves = _refresh(
            args.buck,
            catalog_dir,
            args.engine,
        )

    if not snapshots and not resolves:
        raise SystemExit("catalog: no repository/engine refresh targets found in catalog//...")

    if args.verify:
        print("==> verifying the committed catalog matches", file=sys.stderr)
        status = subprocess.run(
            [
                "git",
                "-C",
                str(catalog_dir),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ":(glob)snapshot/**/*.json",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if status:
            print(status, end="", file=sys.stderr)
            subprocess.run(
                ["git", "-C", str(catalog_dir), "diff", "--", ":(glob)snapshot/**/*.json"],
                check=True,
            )
            raise SystemExit(1)


if __name__ == "__main__":
    main()
