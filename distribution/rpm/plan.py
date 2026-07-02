#!/usr/bin/python3
"""plan — Action 1: resolve a buildroot's package closure over the upstream repo.

Runs *inside* the engine root. Loads each buildroot repository's pinned `repodata/`
(--repo id=dir, a `file://` metadata-only tree), resolves the requested install set's
transitive runtime closure (hard requires only, install_weak_deps=False), and writes the
resolved packages as the *transaction*: a JSON list of {url, sha256, size}, where the url is the
repo's real remote baseurl (--baseurl id=url) + the package's location, and the sha256 + size come
straight from the repodata (size lets download_file skip buck's HEAD probe). `download` (Action 2)
then fetches exactly this subset.

Only metadata is read here (no packages downloaded), so it re-runs whenever a repo's
repodata changes — but the transaction is byte-identical unless *this* buildroot's resolved
closure actually changed, so the download + buildroot cut off rather than rebuilding.
"""

import argparse
import json
import sys
from pathlib import Path

import libdnf5
import libdnf5.conf


def plan(
    repos: list[tuple[str, Path, str]], install: list[str], cachedir: Path
) -> list[dict[str, str | int]]:
    base = libdnf5.base.Base()
    cfg = base.get_config()
    cfg.cachedir = str(cachedir)
    cfg.install_weak_deps = False
    # Load filelists (we pin it): file-path BuildRequires (e.g. /usr/bin/foo) resolve against
    # it, not just the subset primary.xml carries. The snapshot's repomd lists only primary +
    # filelists, so nothing else is fetched regardless.
    cfg.get_optional_metadata_types_option().set(libdnf5.conf.METADATA_TYPE_FILELISTS)
    # Pin the resolution arch (x86_64-only for now), matching buckify — so repo
    # loading + provider selection don't depend on host detection.
    base.get_vars().set("arch", "x86_64")
    base.get_vars().set("basearch", "x86_64")
    base.setup()

    # id -> real remote baseurl, so a resolved package's location becomes a download URL.
    baseurls = {rid: url.rstrip("/") + "/" for rid, _, url in repos}
    sack = base.get_repo_sack()
    for rid, path, _ in repos:
        rc = sack.create_repo(rid).get_config()
        rc.baseurl = f"file://{path}"  # pinned repodata read locally
        rc.get_pkg_gpgcheck_option().set(False)
    sack.load_repos(libdnf5.repo.Repo.Type_AVAILABLE)

    goal = libdnf5.base.Goal(base)
    for name in install:
        goal.add_rpm_install(name)
    tx = goal.resolve()

    problems = tx.get_resolve_logs_as_strings()
    if problems:
        raise SystemExit("buildroot resolution failed:\n  " + "\n  ".join(problems))

    resolved = []
    for tp in tx.get_transaction_packages():
        pkg = tp.get_package()
        chk = pkg.get_checksum()
        if chk.get_type_str() != "sha256":
            raise SystemExit(f"expected sha256 repodata checksum for {pkg.get_nevra()}")
        resolved.append(
            {
                "url": baseurls[pkg.get_repo_id()] + pkg.get_location(),
                "sha256": chk.get_checksum(),
                "size": pkg.get_download_size(),  # lets download_file skip buck's HEAD size probe
            }
        )
    resolved.sort(key=lambda e: e["url"])  # byte-stable transaction
    print(f"plan: resolved {len(resolved)} packages", file=sys.stderr)
    return resolved


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="plan")
    p.add_argument(
        "--repo",
        action="append",
        default=[],
        required=True,
        metavar="ID=DIR",
        help="a buildroot repo as id=dir (a pinned repodata tree); repeatable",
    )
    p.add_argument(
        "--baseurl",
        action="append",
        default=[],
        required=True,
        metavar="ID=URL",
        help="a repo's real remote baseurl as id=url (for download URLs); repeatable",
    )
    p.add_argument("--install", action="append", default=[], help="package/cap to install")
    p.add_argument("--cachedir", default="/var/tmp/plan-cache")
    p.add_argument("--out", required=True, help="output transaction JSON ([{url, sha256}])")
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
    urls = parse_kv(args.baseurl, "--baseurl")
    if dirs.keys() != urls.keys():
        raise SystemExit(f"--repo ids {sorted(dirs)} and --baseurl ids {sorted(urls)} must match")
    repos = [(rid, Path(d).resolve(), urls[rid]) for rid, d in dirs.items()]

    tx = plan(repos, args.install, Path(args.cachedir))
    Path(args.out).write_text(json.dumps(tx, indent=2) + "\n")


if __name__ == "__main__":
    main()
