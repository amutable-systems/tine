"""Refresh catalog snapshots, then resolve engine transactions against those pins.

Engine locks resolve inside an engine built from the committed pins, so the refresh must not
invalidate those pins mid-flight: snapshots first land in the catalog as transitional pins that
carry the previously pinned packages forward, and the pure snapshots replace them only after
every engine lock has been re-resolved against the fresh repodata.

The host orchestrator discovers refresh subtargets and appends only output paths.
Nested Buck reuses the invoking daemon through the inherited isolation directory.
"""

import argparse
import contextlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _buck_out(buck: str, *args: str) -> str:
    return subprocess.run([buck, *args], check=True, capture_output=True, text=True).stdout.strip()


def _refresh_targets(buck: str, kind: str) -> list[str]:
    """The refresh bindings of rule `kind` in the active catalog cell."""
    return sorted(_buck_out(buck, "uquery", f"kind('{kind}', catalog//...)").split())


def _name_of(target: str, suffix: str = "") -> str:
    return target.rsplit(":", 1)[1].removesuffix(suffix)


def _run(buck: str, target: str, args: list[str]) -> None:
    # Mute nested Buck while preserving driver progress on stderr.
    subprocess.run([buck, "-v", "0", "run", target, "--console", "none", "--", *args], check=True)


def _snapshot(buck: str, target: str, catalog_dir: Path, staging_dir: Path) -> tuple[Path, Path]:
    """Stage a repository's pure snapshot, leaving a transitional one in the catalog."""
    repository = _name_of(target)
    print(f"==> snapshotting {repository} (via {target}[snapshot])", file=sys.stderr)
    staged = staging_dir / f"{repository}.json"
    committed = catalog_dir / f"{repository}.json"
    _run(buck, f"{target}[snapshot]", ["--out", str(staged), "--transitional", str(committed)])
    return staged, committed


def _resolve(buck: str, target: str, catalog_dir: Path) -> None:
    engine = _name_of(target)
    print(f"==> resolving {engine} in itself (via {target}[resolve])", file=sys.stderr)
    _run(buck, f"{target}[resolve]", ["--out", str(catalog_dir / f"{engine}.json")])


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="catalog")
    p.add_argument("--catalog-dir", help="catalog dir to (re)generate (default: catalog//)")
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
    # Default to the active catalog; anchor overrides before changing cwd.
    if args.catalog_dir:
        catalog_dir = Path(args.catalog_dir).absolute()
    else:
        catalog_dir = Path(_buck_out(args.buck, "audit", "cell", "catalog", "--paths-only"))
    catalog_dir.mkdir(parents=True, exist_ok=True)

    # Run nested commands from the project root so wrappers resolve consistently.
    with (
        contextlib.chdir(_buck_out(args.buck, "root", "--kind", "project")),
        tempfile.TemporaryDirectory(prefix="catalog-refresh.") as staging,
    ):
        snapshots = _refresh_targets(args.buck, "remote_repository")
        staged = [_snapshot(args.buck, target, catalog_dir, Path(staging)) for target in snapshots]

        resolves = _refresh_targets(args.buck, "engine")
        if args.engine:
            resolves = [t for t in resolves if _name_of(t) in set(args.engine)]
        for target in resolves:
            _resolve(args.buck, target, catalog_dir)

        # Locks now match the fresh repodata; retire the transitional carried-forward pins.
        for pure, committed in staged:
            shutil.move(pure, committed)

    if not snapshots and not resolves:
        raise SystemExit("catalog: no repository/engine refresh targets found in catalog//...")

    if args.verify:
        print("==> verifying the committed catalog matches", file=sys.stderr)
        # git prints the offending diff; just propagate the failure without a traceback.
        proc = subprocess.run(["git", "-C", str(catalog_dir), "diff", "--exit-code", "--", "*.json"])
        if proc.returncode != 0:
            raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
