#!/usr/bin/python3
"""plan — Action 1: resolve a buildroot's package closure over real repos.

Runs *inside* the engine root. Loads the buildroot repositories (--repo id=dir,
each a createrepo'd local repo) as `file://` repos, resolves the requested
install set's transitive runtime closure (hard requires only,
install_weak_deps=False), and writes the resolved package filenames (rpm
basenames) as a sorted JSON list — the *transaction*.

This is the cheap half of the plan/install split: only metadata is read (no
packages downloaded, nothing installed), so it re-runs whenever a repo's metadata
changes — but the transaction is byte-identical unless *this* buildroot's resolved
closure actually changed, so an unrelated repo change re-plans to the same result
and the buildroot (Action 3) cuts off rather than rebuilding.
"""

import argparse
import json
import sys
from pathlib import Path

import libdnf5


def plan(repos: list[tuple[str, Path]], install: list[str], cachedir: Path) -> list[str]:
    base = libdnf5.base.Base()
    cfg = base.get_config()
    cfg.cachedir = str(cachedir)
    cfg.install_weak_deps = False
    # Pin the resolution arch (x86_64-only for now), matching buckify — so repo
    # loading + provider selection don't depend on host detection.
    base.get_vars().set("arch", "x86_64")
    base.get_vars().set("basearch", "x86_64")
    base.setup()

    sack = base.get_repo_sack()
    for rid, path in repos:
        rc = sack.create_repo(rid).get_config()
        rc.baseurl = f"file://{path}"
        rc.get_pkg_gpgcheck_option().set(False)
    sack.load_repos(libdnf5.repo.Repo.Type_AVAILABLE)

    goal = libdnf5.base.Goal(base)
    for name in install:
        goal.add_rpm_install(name)
    tx = goal.resolve()

    problems = tx.get_resolve_logs_as_strings()
    if problems:
        raise SystemExit("buildroot resolution failed:\n  " + "\n  ".join(problems))

    # The resolved closure as rpm filenames (location basenames — createrepo set
    # location_href to the basename), sorted so the transaction is byte-stable.
    names = sorted(
        tp.get_package().get_location().rsplit("/", 1)[-1] for tp in tx.get_transaction_packages()
    )
    print(f"plan: resolved {len(names)} packages", file=sys.stderr)
    return names


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="plan")
    p.add_argument(
        "--repo",
        action="append",
        default=[],
        required=True,
        metavar="ID=DIR",
        help="a buildroot repository as id=dir (a local repo tree); repeatable",
    )
    p.add_argument("--install", action="append", default=[], help="package/cap to install")
    p.add_argument("--cachedir", default="/var/tmp/plan-cache")
    p.add_argument("--out", required=True, help="output transaction JSON (resolved filenames)")
    args = p.parse_args(argv)

    repos: list[tuple[str, Path]] = []
    for spec in args.repo:
        rid, sep, d = spec.partition("=")
        if not sep or not rid:
            raise SystemExit(f"--repo expects id=dir, got {spec!r}")
        repos.append((rid, Path(d).resolve()))

    names = plan(repos, args.install, Path(args.cachedir))
    Path(args.out).write_text(json.dumps(names, indent=2) + "\n")


if __name__ == "__main__":
    main()
