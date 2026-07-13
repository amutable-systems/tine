#!/usr/bin/python3
"""plan — Action 1: resolve a package closure over the upstream repo.

Runs *inside* the engine root. Loads each repository's pinned `repodata/`
(--repo id=dir, a `file://` metadata-only tree), resolves the requested install set's
transitive runtime closure (hard requires only, install_weak_deps=False), and writes the
resolved packages as the *transaction*. Remote entries are `{source: "repo", repo, pkgid,
nevra}`: `(repo, pkgid)` alone selects an artifact from the authoritative repository pool.
Local entries add `location: <input-directory index>/<filename>` so they project directly
from an RPM directory built in this graph.

Two resolution bases: against an empty root (a fresh buildroot / a base image
layer), or — with `--lower` — against an existing installed tree (a child image
layer installing *more* packages). For the latter the parent's delta stack is
overlay-merged read-only (an ephemeral upper, never captured) and used as the
installroot, so libdnf5 loads its rpmdb as the system repo and resolves
incrementally: the transaction lists only the *inbound* packages, with
already-installed ones satisfying dependencies instead of reappearing.

Only metadata is read here (no packages downloaded), so it re-runs whenever a repo's
repodata changes — but the transaction is byte-identical unless *this* closure actually
changed, so the download + install cut off rather than rebuilding.

Also the engine-closure resolver (reindeer-style, run on refresh, NOT during builds):
an engine's `[resolve]` sub-target binds this same driver — inside that engine, over
its repos' pinned repodata — and the orchestrator (//distribution:buckify) commits the
transaction as the engine's lock fragment. The lock *is* a transaction: engine
bootstrap downloads straight from it, consulting no repodata. One solver, two bindings.

Loading a repo means parsing its XML into libdnf5's .solv cache — for Fedora ~1GB of
XML (filelists is 3/4 of it), re-paid by every plan action since the sandbox cachedir
is ephemeral. The `make-cache` command runs just the load, with `--out` as the cachedir:
the distribution rule captures it per repo as a buck artifact, and every solve seeds its
cachedir from those (`--cache`), so the XML is parsed once per repo, not once per plan.
"""

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path

import libdnf5
import libdnf5.comps
import libdnf5.conf
import rootfs

# Providers we never want in a closure: 32-bit multilib duplicates. The resolution arch
# is pinned, so a 32-bit provider in the transaction means the solve went wrong; fail
# loudly rather than silently shipping an .i686.
MULTILIB_ARCHES = ("i686", "i386", "i586")


def load_base(
    repos: list[tuple[str, Path, int]],
    cachedir: Path,
    installroot: Path | None,
    arch: str,
    seeds: list[Path] | None = None,
) -> libdnf5.base.Base:
    """A Base with `repos` ((id, pinned-repodata dir, dnf priority) triples) loaded, ready to solve.

    With `seeds` (per-repo cache dirs from `make-cache`), libdnf5's root-cache clone is
    pointed at them via system_cachedir: a repo whose working cache is empty copies the
    seeded repodata + .solv in and mmaps it instead of re-parsing the XML. libdnf5 keys
    each seed subdir by the repo's file:// baseurl (the repo dir's absolute path), so a
    stale or foreign seed just misses and the load falls back to the parse — slower,
    never wrong.
    """
    base = libdnf5.base.Base()
    cfg = base.get_config()
    cfg.cachedir = str(cachedir)
    cfg.install_weak_deps = False
    if installroot is not None:
        cfg.installroot = str(installroot)
    if seeds:
        # One system_cachedir holding every seed's `<id>-<hash>` subdir, by symlink —
        # libdnf5 only ever copies *out* of it, so read-only buck outputs are fine.
        seed_root = cachedir.parent / (cachedir.name + "-seed")
        seed_root.mkdir(parents=True, exist_ok=True)
        for seed in seeds:
            for sub in sorted(seed.iterdir()):
                (seed_root / sub.name).symlink_to(sub)
        cfg.system_cachedir = str(seed_root)
    # Freshness is buck's problem (the repodata and seeds are pinned action inputs),
    # not wall-clock age's; never expire a cache that validates.
    cfg.metadata_expire = -1
    # Load filelists + comps (both pinned): file-path BuildRequires (e.g. /usr/bin/foo) resolve
    # against filelists, not just the subset primary.xml carries; `@group` install specs (the
    # buildroot base) expand against comps. The snapshot's repomd lists only the pinned
    # streams, so nothing else is fetched regardless.
    cfg.get_optional_metadata_types_option().set(
        f"{libdnf5.conf.METADATA_TYPE_FILELISTS},{libdnf5.conf.METADATA_TYPE_COMPS}"
    )
    # Pin the resolution arch (x86_64-only for now) so repo loading + provider
    # selection don't depend on host detection.
    base.get_vars().set("arch", arch)
    base.get_vars().set("basearch", arch)
    base.setup()

    sack = base.get_repo_sack()
    for rid, path, priority in repos:
        rc = sack.create_repo(rid).get_config()
        rc.baseurl = f"file://{path}"  # pinned repodata read locally
        rc.get_pkg_gpgcheck_option().set(False)
        # dnf semantics: lower number wins, across versions -- an outranking repo's package is
        # taken even when another repo carries a newer NEVRA (how our own builds beat upstream).
        rc.get_priority_option().set(priority)
    if installroot is not None:
        # The installroot's rpmdb too, so installed packages provide instead of re-resolving.
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
) -> list[dict[str, str]]:
    base = load_base(repos, cachedir, installroot, arch, seeds)

    goal = libdnf5.base.Goal(base)
    # add_install (not add_rpm_install) so `@group` specs resolve too. Groups take only
    # their mandatory members — mock's `@buildsys-build` semantics.
    settings = libdnf5.base.GoalJobSettings()
    settings.set_group_package_types(libdnf5.comps.PackageType_MANDATORY)
    for spec in install:
        goal.add_install(spec, settings)
    tx = goal.resolve()

    # Resolving against an installed base (the buildroot's BR delta, an incremental image
    # layer) reports each requested spec the base already satisfies as ALREADY_INSTALLED —
    # benign: the base provides it, so it just doesn't reappear in the transaction. BuildRequires
    # overlap the base heavily, so fail only on the real problems, not these notices.
    problems = [
        log.to_string()
        for log in tx.get_resolve_logs()
        if log.get_problem() != libdnf5.base.GoalProblem_ALREADY_INSTALLED
    ]
    if problems:
        raise SystemExit("plan resolution failed:\n  " + "\n  ".join(problems))

    # The inbound half of the transaction: outbound/kept items (REPLACED, REASON_CHANGE —
    # possible only when resolving against an installroot) aren't packages to download.
    resolved = []
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
        entry = {
            "nevra": pkg.get_nevra(),
            "repo": rid,
            "pkgid": pkgid,
            "source": "local" if rid in local_repos else "repo",
        }
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
        # The priority only orders the solve; any value caches the same.
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
            # No upperdir: an ephemeral upper, so libdnf5's cache writes into the
            # installroot land in scratch and the merge is effectively read-only.
            installroot = stack.enter_context(rootfs.rootfs("/installroot", lowers=args.lower))
        tx = plan(repos, args.install, Path(args.cachedir), installroot, args.arch, seeds, local_repos)
    # Explicit encoding/newline: the `[resolve]` binding commits this output as the
    # engine lock, so it must be byte-identical regardless of locale or platform.
    Path(args.out).write_text(json.dumps(tx, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
