"""Refresh pure catalog snapshots, then resolve engine transactions against those pins.

Remote engine-lock entries retain their package transports, so a repository's package pool keeps
the committed engine available after its repodata advances.

The host orchestrator discovers refresh subtargets and appends only output paths.
Nested Buck reuses the invoking daemon through the inherited isolation directory.
"""

import argparse
import contextlib
import subprocess
import sys
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
