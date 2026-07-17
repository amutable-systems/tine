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
from typing import Literal, NamedTuple, NotRequired, TypedDict

import libdnf5
import libdnf5.comps
import libdnf5.conf
from util import atomic_text_writer

import rootfs

# A multilib package in a pinned-arch transaction indicates a bad solve.
MULTILIB_ARCHES = ("i686", "i386", "i586")


class Repository(NamedTuple):
    id: str
    path: Path
    priority: int
    baseurl: str | None


class TransactionPackage(TypedDict):
    nevra: str
    repo: str
    pkgid: str
    source: Literal["local", "repo"]
    location: NotRequired[str]
    size: NotRequired[int]
    url: NotRequired[str]


def load_repositories(path: Path) -> list[Repository]:
    """Load repository tuples written by Buck's `write_json()`."""
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"{path}: repository manifest must be a list")
    repositories = []
    for index, value in enumerate(data):
        if not isinstance(value, list) or len(value) != 4:
            raise SystemExit(f"{path}: repository {index} must be [id, path, priority, baseurl]")
        rid, directory, priority, baseurl = value
        if (
            not isinstance(rid, str)
            or not rid
            or not isinstance(directory, str)
            or not directory
            or type(priority) is not int
            or not isinstance(baseurl, str | None)
        ):
            raise SystemExit(f"{path}: repository {index} has invalid fields")
        repositories.append(Repository(rid, Path(directory).resolve(), priority, baseurl))
    return repositories


def write_transaction(path: Path, transaction: list[TransactionPackage]) -> None:
    """Atomically write deterministic transaction JSON."""
    with atomic_text_writer(path) as output:
        json.dump(transaction, output, indent=2)
        output.write("\n")


def load_base(
    repos: list[Repository],
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
    for repo in repos:
        rc = sack.create_repo(repo.id).get_config()
        rc.baseurl = f"file://{repo.path}"  # pinned repodata read locally
        rc.get_pkg_gpgcheck_option().set(False)
        # Lower priorities win even when another repository has a newer NEVRA.
        rc.get_priority_option().set(repo.priority)
    if installroot is not None:
        # Load the rpmdb so installed packages satisfy dependencies.
        sack.load_repos()
    else:
        sack.load_repos(libdnf5.repo.Repo.Type_AVAILABLE)
    return base


def plan(
    repos: list[Repository],
    install: list[str],
    cachedir: Path,
    installroot: Path | None,
    arch: str,
    seeds: list[Path],
) -> list[TransactionPackage]:
    base = load_base(repos, cachedir, installroot, arch, seeds)
    repositories = {repo.id: repo for repo in repos}

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
        repo = repositories[rid]
        pkgid = chk.get_checksum().lower()
        if len(pkgid) != 64 or any(character not in "0123456789abcdef" for character in pkgid):
            raise SystemExit(f"invalid sha256 pkgid for {pkg.get_nevra()}: {pkgid!r}")
        entry = TransactionPackage(
            nevra=pkg.get_nevra(),
            repo=rid,
            pkgid=pkgid,
            source="local" if repo.baseurl is None else "repo",
        )
        if repo.baseurl is None:
            entry["location"] = pkg.get_location()
        else:
            location = pkg.get_location()
            size = pkg.get_download_size()
            if size <= 0:
                raise SystemExit(f"invalid download size for {pkg.get_nevra()}: {size}")
            entry["size"] = size
            entry["url"] = repo.baseurl.rstrip("/") + "/" + location.lstrip("/")
        resolved.append(entry)
    resolved.sort(
        key=lambda entry: (
            entry["repo"],
            entry["nevra"],
            entry["pkgid"],
            entry.get("location", ""),
            entry.get("url", ""),
        )
    )
    print(f"plan: resolved {len(resolved)} packages", file=sys.stderr)
    return resolved


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repositories",
        required=True,
        help="JSON repository tuples: [id, pinned metadata directory, priority, remote base URL or null]",
    )
    common.add_argument("--arch", default="x86_64", help="the resolution arch")

    p = argparse.ArgumentParser(prog="plan")
    sub = p.add_subparsers(dest="command", required=True)

    solve = sub.add_parser("solve", parents=[common], help="resolve the closure, writing the transaction")
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
        help="output transaction JSON (remote adds url/size; local adds location)",
    )

    cache = sub.add_parser("make-cache", parents=[common], help="just load the repos (no solve)")
    cache.add_argument("--out", required=True, help="output cache dir, reusable via `solve --cache`")

    args = p.parse_args(argv)
    repos = load_repositories(Path(args.repositories))

    if args.command == "make-cache":
        out = Path(args.out).resolve()
        out.mkdir(parents=True, exist_ok=True)
        load_base(repos, out, None, args.arch)
        print(f"plan: cached {len(repos)} repo(s)", file=sys.stderr)
        return

    seeds = [Path(c).resolve() for c in args.cache]

    with ExitStack() as stack:
        installroot = None
        if args.lower:
            # An ephemeral upper keeps the lower stack unchanged.
            installroot = stack.enter_context(rootfs.rootfs("/installroot", lowers=args.lower))
        tx = plan(
            repos,
            args.install,
            Path(args.cachedir),
            installroot,
            args.arch,
            seeds,
        )
    write_transaction(Path(args.out), tx)


if __name__ == "__main__":
    main()
