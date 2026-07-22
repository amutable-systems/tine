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

DEFAULT_CATALOG = "tine//catalog"
ENGINE_LABEL = "tine:engine"
RPM_REMOTE_REPOSITORY_LABEL = "tine:rpm-remote-repository"


def _buck_out(buck: str, *args: str) -> str:
    return subprocess.run([buck, *args], check=True, capture_output=True, text=True).stdout.strip()


def _catalog_pattern(catalog: str) -> str:
    package = catalog.removesuffix(":")
    if "//" not in package or ":" in package or "..." in package:
        raise SystemExit(f"catalog: expected a package label, got {catalog!r}")
    return f"{package}:"


def _targets_with_label(buck: str, catalog: str, label: str) -> list[str]:
    """Targets carrying `label` in the selected catalog package."""
    return sorted(_buck_out(buck, "uquery", f"attrfilter(labels, '{label}', {catalog})").split())


def _name_of(target: str) -> str:
    return target.rsplit(":", 1)[1]


def _catalog_directory(buck: str, targets: list[str]) -> Path:
    packages = {target.rsplit(":", 1)[0] for target in targets}
    if len(packages) != 1:
        raise SystemExit(f"catalog: expected targets in one package, found {sorted(packages)}")
    cell, separator, package = packages.pop().partition("//")
    if not separator or not cell:
        raise SystemExit("catalog: Buck returned a target without a canonical cell")
    cell_root = Path(_buck_out(buck, "audit", "cell", cell, "--paths-only"))
    return cell_root / package


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


def _rpmrepo_repositories(buck: str, catalog: str) -> dict[str, tuple[str, str]]:
    """Map repository targets carrying an rpmrepo pin in their metadata to (mirror, snapshot)."""
    out = _buck_out(
        buck,
        "uquery",
        "--json",
        "--output-attribute=^metadata$",
        f"attrfilter(labels, '{RPM_REMOTE_REPOSITORY_LABEL}', {catalog})",
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


def _advance_snapshots(buck: str, catalog: str, catalog_dir: Path) -> None:
    """Advance rpmrepo-mirrored repositories to the newest snapshot their gateway enumerates."""
    declaration = catalog_dir / "BUCK"
    for target, (mirror, current) in sorted(_rpmrepo_repositories(buck, catalog).items()):
        repository = _name_of(target)
        wanted = _newest_snapshot(repository, mirror, _series(current))
        if wanted == current:
            continue
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


def _repositories_for_engines(buck: str, engines: list[str]) -> list[str]:
    """rpm-remote-repository targets reachable from the given engine targets."""
    engine_set = " ".join(engines)
    query = f"attrfilter(labels, '{RPM_REMOTE_REPOSITORY_LABEL}', deps(set({engine_set})))"
    return sorted(_buck_out(buck, "uquery", query).split())


def _refresh(
    buck: str,
    catalog: str,
    selected_engines: list[str] | None,
    advance_snapshots: bool,
) -> tuple[Path, list[Path]]:
    """Snapshot repositories and resolve selected engines; return the catalog dir and written files.

    Selecting engines also scopes the snapshotted repositories to those the engines depend on, so a
    partial refresh or verify never touches repositories outside the selection (e.g. the deliberately
    unpinned CentOS mirrors, which drift and are not meant to be verified).
    """
    all_resolves = _targets_with_label(buck, catalog, ENGINE_LABEL)
    resolves = _select_engines(all_resolves, selected_engines)

    if selected_engines is None:
        snapshots = _targets_with_label(buck, catalog, RPM_REMOTE_REPOSITORY_LABEL)
    else:
        snapshots = _repositories_for_engines(buck, resolves)
    targets = all_resolves + snapshots
    if not targets:
        raise SystemExit(f"catalog: no repository/engine refresh targets found in {catalog}")
    catalog_dir = _catalog_directory(buck, targets)
    if advance_snapshots:
        _advance_snapshots(buck, catalog, catalog_dir)

    written = []
    for target in snapshots:
        _snapshot(buck, target, catalog_dir)
        written.append(_repository_snapshot_path(catalog_dir, target))

    for target in resolves:
        _resolve(buck, target, catalog_dir)
        written.append(_engine_snapshot_path(catalog_dir, target))

    return catalog_dir, written


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="catalog")
    p.add_argument(
        "catalog",
        nargs="?",
        default=DEFAULT_CATALOG,
        help=f"catalog package to refresh (default: {DEFAULT_CATALOG})",
    )
    p.add_argument(
        "--buck", default="buck", help="buck binary to nest (aliases pass the pinned one; default: PATH)"
    )
    p.add_argument(
        "--engine",
        action="append",
        help="only (re)resolve these engines and snapshot the repositories they depend on; default: all",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="assert the committed catalog matches what the pinned resolvers produce (CI)",
    )
    args = p.parse_args(argv)
    catalog = _catalog_pattern(args.catalog)

    # Run nested commands from the project root so wrappers resolve consistently.
    with contextlib.chdir(_buck_out(args.buck, "root", "--kind", "project")):
        catalog_dir, written = _refresh(
            args.buck,
            catalog,
            args.engine,
            advance_snapshots=not args.verify,
        )

    if args.verify:
        print("==> verifying the committed catalog matches", file=sys.stderr)
        # Check exactly the files this run regenerated, so a scoped verify ignores everything else.
        pathspecs = sorted(str(path.relative_to(catalog_dir)) for path in written)
        status = subprocess.run(
            [
                "git",
                "-C",
                str(catalog_dir),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                *pathspecs,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if status:
            print(status, end="", file=sys.stderr)
            subprocess.run(
                ["git", "-C", str(catalog_dir), "diff", "--", *pathspecs],
                check=True,
            )
            raise SystemExit(1)


if __name__ == "__main__":
    main()
