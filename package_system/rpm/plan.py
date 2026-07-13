#!/usr/bin/python3
"""Resolve package transactions or prebuild libdnf5 repository caches.

Solves against pinned metadata and optionally an existing lower stack. Remote
packages are identified by repository and pkgid; local packages also record their
input location. `make-cache` amortizes metadata parsing across solve actions.
"""

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

import libdnf5
import libdnf5.comps
import libdnf5.conf

import rootfs

# A multilib package in a pinned-arch transaction indicates a bad solve.
MULTILIB_ARCHES = ("i686", "i386", "i586")


class TransactionPackage(TypedDict):
    nevra: str
    repo: str
    pkgid: str
    source: Literal["local", "repo"]
    location: NotRequired[str]


def load_base(
    repos: list[tuple[str, Path, int]],
    cachedir: Path,
    installroot: Path | None,
    arch: str,
    seeds: list[Path] | None = None,
) -> libdnf5.base.Base:
    """Load pinned repositories, optionally seeding libdnf5's parsed metadata cache."""
    base = libdnf5.base.Base()
    cfg = base.get_config()
    cfg.cachedir = str(cachedir)
    cfg.install_weak_deps = False
    if installroot is not None:
        cfg.installroot = str(installroot)
    if seeds:
        # libdnf5 only copies out of system_cachedir, so Buck outputs can stay read-only.
        seed_root = cachedir.parent / (cachedir.name + "-seed")
        seed_root.mkdir(parents=True, exist_ok=True)
        for seed in seeds:
            for sub in sorted(seed.iterdir()):
                (seed_root / sub.name).symlink_to(sub)
        cfg.system_cachedir = str(seed_root)
    # Buck pins freshness through action inputs, not wall-clock age.
    cfg.metadata_expire = -1
    # Filelists resolves path dependencies; comps expands `@group` install specs.
    cfg.get_optional_metadata_types_option().set(
        f"{libdnf5.conf.METADATA_TYPE_FILELISTS},{libdnf5.conf.METADATA_TYPE_COMPS}"
    )
    # Do not let host detection affect provider selection.
    base.get_vars().set("arch", arch)
    base.get_vars().set("basearch", arch)
    base.setup()

    sack = base.get_repo_sack()
    for rid, path, priority in repos:
        rc = sack.create_repo(rid).get_config()
        rc.baseurl = f"file://{path}"  # pinned repodata read locally
        rc.get_pkg_gpgcheck_option().set(False)
        # Lower priorities win even when another repository has a newer NEVRA.
        rc.get_priority_option().set(priority)
    if installroot is not None:
        # Load the rpmdb so installed packages satisfy dependencies.
        sack.load_repos()
    else:
        sack.load_repos(libdnf5.repo.Repo.Type_AVAILABLE)
    return base


def plan(
    repos: list[tuple[str, Path, int]],
    install: list[str],
    cachedir: Path,
    installroot: Path | None,
    arch: str,
    seeds: list[Path],
    local_repos: set[str],
) -> list[TransactionPackage]:
    base = load_base(repos, cachedir, installroot, arch, seeds)

    goal = libdnf5.base.Goal(base)
    # add_install supports groups; use only their mandatory members.
    settings = libdnf5.base.GoalJobSettings()
    settings.set_group_package_types(libdnf5.comps.PackageType_MANDATORY)
    for spec in install:
        goal.add_install(spec, settings)
    tx = goal.resolve()

    # An installed lower legitimately satisfies requested packages without adding them.
    problems = [
        log.to_string()
        for log in tx.get_resolve_logs()
        if log.get_problem() != libdnf5.base.GoalProblem_ALREADY_INSTALLED
    ]
    if problems:
        raise SystemExit("plan resolution failed:\n  " + "\n  ".join(problems))

    # Only inbound transaction items need downloading.
    resolved: list[TransactionPackage] = []
    for tp in tx.get_transaction_packages():
        if not libdnf5.transaction.transaction_item_action_is_inbound(tp.get_action()):
            continue
        pkg = tp.get_package()
        if pkg.get_arch() in MULTILIB_ARCHES:
            raise SystemExit(f"refusing multilib package {pkg.get_nevra()} (32-bit in a {arch} closure)")
        chk = pkg.get_checksum()
        if chk.get_type_str() != "sha256":
            raise SystemExit(f"expected sha256 repodata checksum for {pkg.get_nevra()}")
        rid = pkg.get_repo_id()
        pkgid = chk.get_checksum().lower()
        if len(pkgid) != 64 or any(character not in "0123456789abcdef" for character in pkgid):
            raise SystemExit(f"invalid sha256 pkgid for {pkg.get_nevra()}: {pkgid!r}")
        entry = TransactionPackage(
            nevra=pkg.get_nevra(),
            repo=rid,
            pkgid=pkgid,
            source="local" if rid in local_repos else "repo",
        )
        if rid in local_repos:
            entry["location"] = pkg.get_location()
        resolved.append(entry)
    resolved.sort(
        key=lambda entry: (entry["repo"], entry["nevra"], entry["pkgid"], entry.get("location", ""))
    )
    print(f"plan: resolved {len(resolved)} packages", file=sys.stderr)
    return resolved


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo",
        action="append",
        default=[],
        required=True,
        metavar="ID=DIR",
        help="a repo as id=dir (a pinned repodata tree); repeatable",
    )
    common.add_argument("--arch", default="x86_64", help="the resolution arch")

    p = argparse.ArgumentParser(prog="plan")
    sub = p.add_subparsers(dest="command", required=True)

    solve = sub.add_parser("solve", parents=[common], help="resolve the closure, writing the transaction")
    solve.add_argument(
        "--local-repo",
        action="append",
        default=[],
        metavar="ID",
        help="a --repo whose packages are local build artifacts; repeatable",
    )
    solve.add_argument(
        "--priority",
        action="append",
        default=[],
        metavar="ID=N",
        help="a repo's dnf priority as id=number (lower wins; default 99); repeatable",
    )
    solve.add_argument(
        "--install", action="append", default=[], required=True, help="package/cap to install"
    )
    solve.add_argument(
        "--lower",
        action="append",
        default=[],
        help="installed-tree delta (bottom..top); resolve against the merge instead of empty",
    )
    solve.add_argument(
        "--cache",
        action="append",
        default=[],
        help="a prebuilt repo cache dir (a make-cache output) to seed the solve from; repeatable",
    )
    solve.add_argument("--cachedir", default="/var/tmp/plan-cache")
    solve.add_argument(
        "--out",
        required=True,
        help="output transaction JSON (remote: source/repo/pkgid/nevra; local adds location)",
    )

    cache = sub.add_parser("make-cache", parents=[common], help="just load the repos (no solve)")
    cache.add_argument("--out", required=True, help="output cache dir, reusable via `solve --cache`")

    args = p.parse_args(argv)

    def parse_kv(specs: list[str], what: str) -> dict[str, str]:
        out = {}
        for spec in specs:
            k, sep, v = spec.partition("=")
            if not sep or not k:
                raise SystemExit(f"{what} expects id=value, got {spec!r}")
            out[k] = v
        return out

    dirs = parse_kv(args.repo, "--repo")

    if args.command == "make-cache":
        out = Path(args.out).resolve()
        out.mkdir(parents=True, exist_ok=True)
        # Priority does not affect cached metadata.
        load_base([(rid, Path(d).resolve(), 99) for rid, d in dirs.items()], out, None, args.arch)
        print(f"plan: cached {len(dirs)} repo(s)", file=sys.stderr)
        return

    priorities = parse_kv(args.priority, "--priority")
    if unknown := priorities.keys() - dirs.keys():
        raise SystemExit(f"--priority for unknown repo ids {sorted(unknown)}")
    local_repos = set(args.local_repo)
    if unknown := local_repos - dirs.keys():
        raise SystemExit(f"--local-repo names unknown repo ids {sorted(unknown)}")
    repos = [(rid, Path(d).resolve(), int(priorities.get(rid, "99"))) for rid, d in dirs.items()]
    seeds = [Path(c).resolve() for c in args.cache]

    with ExitStack() as stack:
        installroot = None
        if args.lower:
            # An ephemeral upper keeps the lower stack unchanged.
            installroot = stack.enter_context(rootfs.rootfs("/installroot", lowers=args.lower))
        tx = plan(repos, args.install, Path(args.cachedir), installroot, args.arch, seeds, local_repos)
    # Engine locks must be byte-identical across locales and platforms.
    Path(args.out).write_text(json.dumps(tx, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
