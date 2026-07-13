"""Refresh catalog snapshots, then resolve engine transactions against those pins.

The host orchestrator discovers refresh subtargets and appends only their output path.
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
    """The refresh bindings of rule `kind` in the active catalog cell."""
    return sorted(_buck_out(buck, "uquery", f"kind('{kind}', catalog//...)").split())


def _name_of(target: str, suffix: str = "") -> str:
    return target.rsplit(":", 1)[1].removesuffix(suffix)


def _run(buck: str, target: str, args: list[str]) -> None:
    # Mute nested Buck while preserving driver progress on stderr.
    subprocess.run([buck, "-v", "0", "run", target, "--console", "none", "--", *args], check=True)


def _snapshot(buck: str, target: str, catalog_dir: Path) -> None:
    repository = _name_of(target)
    print(f"==> snapshotting {repository} (via {target}[snapshot])", file=sys.stderr)
    _run(buck, f"{target}[snapshot]", ["--out", str(catalog_dir / f"{repository}.json")])


def _resolve(buck: str, target: str, catalog_dir: Path) -> None:
    engine = _name_of(target)
    print(f"==> resolving {engine} in itself (via {target}[resolve])", file=sys.stderr)
    _run(buck, f"{target}[resolve]", ["--out", str(catalog_dir / f"{engine}.json")])


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="buckify")
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
    with contextlib.chdir(_buck_out(args.buck, "root", "--kind", "project")):
        snapshots = _refresh_targets(args.buck, "remote_repository")
        for target in snapshots:
            _snapshot(args.buck, target, catalog_dir)

        resolves = _refresh_targets(args.buck, "engine")
        if args.engine:
            resolves = [t for t in resolves if _name_of(t) in set(args.engine)]
        for target in resolves:
            _resolve(args.buck, target, catalog_dir)

    if not snapshots and not resolves:
        raise SystemExit("buckify: no repository/engine refresh targets found in catalog//...")

    if args.verify:
        print("==> verifying the committed catalog matches", file=sys.stderr)
        # git prints the offending diff; just propagate the failure without a traceback.
        proc = subprocess.run(["git", "-C", str(catalog_dir), "diff", "--exit-code", "--", "*.json"])
        if proc.returncode != 0:
            raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
