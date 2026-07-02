#!/usr/bin/python3
"""buckify-rpm — the rpm resolver (reindeer-style, run on refresh, NOT during
builds): the rpm implementation of the format-neutral `resolve` interface that the
host orchestrator (//distribution:buckify) drives, one distribution at a time.

libdnf5 is the single engine for resolution/provides/metadata, used here only as
*tooling*: `resolve` resolves a distribution's transitive runtime closures against pinned
repodata. The repodata is never a build input; the build graph consumes only the
generated http_file stanzas (url + sha256). It must run inside *this distribution's*
engine root (the host orchestrator binds each distribution's buckify target to its
engine), so the resolution uses the pinned libdnf5 that builds the distribution.

`buckify-rpm resolve --distribution <name> --catalog-dir <dir>`: reads <dir>/manifest.toml,
resolves <name>'s engine closure and snapshots its buildroot repodata, and writes the
fragment <dir>/<name>.json — a plain JSON object (package_format, engine, buildroot,
buildroot_repos) that buck2 loads natively at load time. The buildroot itself is not
resolved here: the buildroot repos are pinned *repodata* snapshots (repomd + primary +
filelists), and each package's per-build buildroot is resolved + downloaded lazily at build
time against them, so any BuildRequires is available without curating a pool. The host
orchestrator amalgamates the fragments into generated.bzl; re-running against the same pins
reproduces the fragment byte-for-byte.
"""

import argparse
import json
import shutil
import sys
import tempfile
import tomllib
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Self

import libdnf5

# Providers we never want pulled into a seed/buildroot closure: 32-bit multilib
# duplicates. Excluded pool-wide so the transitive solve never selects them.
MULTILIB_ARCHES = ("i686", "i386", "i586")


class Pool:
    """A loaded libdnf5 Base over one distribution's pinned repositories.

    A distribution is several repos (e.g. Fedora's frozen GA `releases` tree plus the
    live `updates` tree); the solver resolves against their union and picks the
    highest NEVRA, so updates shadow GA exactly as on a real install. Each
    package's download URL is its *own* repo's baseurl + relative location.
    """

    def __init__(
        self,
        repositories: list[tuple[str, str]],  # (id, baseurl) in resolution order
        distribution_id: str = "seed",
        arch: str = "x86_64",
        cachedir: str | None = None,
    ) -> None:
        self.base = libdnf5.base.Base()
        cfg = self.base.get_config()
        self._tmp: str | None = None
        if cachedir is None:
            self._tmp = tempfile.mkdtemp(prefix="buckify-seed-")
            cachedir = self._tmp
        # setup()/load_repos() can raise after the temp dir exists but before the
        # caller's `with` takes ownership; clean it up so a failure doesn't leak.
        try:
            cfg.cachedir = cachedir
            cfg.installroot = str(Path(cachedir) / "installroot")
            # Buildroots match Koji: no weak deps anywhere.
            cfg.install_weak_deps = False
            self.base.get_vars().set("arch", arch)
            self.base.get_vars().set("basearch", "x86_64")
            self.base.setup()

            self.arch = arch
            self.distribution_id = distribution_id
            # repo id -> baseurl, so pkg_url can resolve a package against the repo
            # it actually came from.
            self.baseurls = {rid: baseurl.rstrip("/") + "/" for rid, baseurl in repositories}

            sack = self.base.get_repo_sack()
            for rid, baseurl in self.baseurls.items():
                rc = sack.create_repo(rid).get_config()
                rc.baseurl = baseurl
                rc.get_pkg_gpgcheck_option().set(False)
            sack.load_repos(libdnf5.repo.Repo.Type_AVAILABLE)
            self.sack = sack
        except BaseException:
            if self._tmp:
                shutil.rmtree(self._tmp, ignore_errors=True)
            raise

    def query(self) -> libdnf5.rpm.PackageQuery:
        return libdnf5.rpm.PackageQuery(self.base)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._tmp and Path(self._tmp).is_dir():
            shutil.rmtree(self._tmp, ignore_errors=True)


def pkg_url(pool: Pool, pkg: libdnf5.rpm.Package) -> str:
    """Absolute download URL = the package's own repo baseurl + relative location."""
    return pool.baseurls[pkg.get_repo_id()] + pkg.get_location()


def pkg_sha256(pkg: libdnf5.rpm.Package) -> str:
    chk = pkg.get_checksum()
    kind = chk.get_type_str()
    if kind != "sha256":
        raise SystemExit(f"expected sha256 repodata checksum, got {kind} for {pkg.get_nevra()}")
    return chk.get_checksum()


def _target_name(nevra: str) -> str:
    """Stable buck target name for a seed rpm. NEVRA is unique within a pin."""
    return nevra.replace(":", "-").replace("+", "_plus_").replace("~", "_t_")


def resolve_closure(pool: Pool, packages: list[str]) -> list[libdnf5.rpm.Package]:
    """Transitive runtime closure of `packages` as a list of libdnf5 Packages.

    Hard requires only (install_weak_deps=False set on the Base). Multilib
    duplicates are excluded by asserting nothing in the closure is 32-bit —
    they should never be pulled for an x86_64-only install; if one ever is, the
    seed fails loudly rather than silently shipping an .i686.
    """
    goal = libdnf5.base.Goal(pool.base)
    for name in packages:
        goal.add_rpm_install(name)
    tx = goal.resolve()

    problems = tx.get_resolve_logs_as_strings()
    if problems:
        raise SystemExit("seed resolution failed:\n  " + "\n  ".join(problems))

    pkgs: list[libdnf5.rpm.Package] = []
    for tp in tx.get_transaction_packages():
        pkg = tp.get_package()
        if pkg.get_arch() in MULTILIB_ARCHES:
            raise SystemExit(
                f"refusing to seed multilib package {pkg.get_nevra()} (32-bit in an x86_64 closure)"
            )
        pkgs.append(pkg)
    return pkgs


def expand_groups(pool: Pool, groupids: list[str]) -> list[str]:
    """Mandatory package names of the named comps groups (e.g. @buildsys-build).

    The repo's comps (loaded by load_repos) is the authoritative source for the
    buildroot base, so it's never hand-copied. Only mandatory members are taken —
    that's what mock installs for `@buildsys-build`.
    """
    names: set[str] = set()
    for gid in groupids:
        gq = libdnf5.comps.GroupQuery(pool.base)
        gq.filter_groupid([gid])
        # SWIG makes the query iterable at runtime but ty can't see __iter__; the
        # annotation restores the element type so the loop body type-checks.
        groups: list[libdnf5.comps.Group] = list(gq)  # ty: ignore
        if not groups:
            raise SystemExit(f"comps group {gid!r} not found in repo {pool.distribution_id}")
        for group in groups:
            for pkg in group.get_packages_of_type(libdnf5.comps.PackageType_MANDATORY):
                names.add(pkg.get_name())
    return sorted(names)


# A rendered rpm: (buck target name, download url, sha256, download size in bytes, source rpm
# name). The size lets http_file skip buck's HEAD size probe. The pool resolves these while it's
# open, so they survive after it closes.
type Entry = tuple[str, str, str, int, str]


def entries(pool: Pool, pkgs: list[libdnf5.rpm.Package]) -> list[Entry]:
    """Freeze resolved packages into Entry tuples (sorted by NEVRA, byte-stable)."""
    return [
        (
            _target_name(p.get_nevra()),
            pkg_url(pool, p),
            pkg_sha256(p),
            p.get_download_size(),
            p.get_source_name(),
        )
        for p in sorted(pkgs, key=lambda p: p.get_nevra())
    ]


def render_fragment(dist: dict) -> str:
    """Render one distribution's fragment as a plain JSON object buck2 loads natively
    (`load(":<name>.json", ... = "value")`) — no Starlark wrapper. Tuples become JSON
    arrays the consumers destructure as lists; sort_keys + pre-sorted entries make it
    byte-identical for identical pins. The field schema is documented once, in the
    amalgamation (generated.bzl), since JSON can't carry comments."""
    return json.dumps(dist, indent=2, sort_keys=True) + "\n"


def _repos(distribution: dict) -> list[tuple[str, str]]:
    return [(r["id"], r["baseurl"]) for r in distribution["repositories"]]


# repomd.xml's namespace (the <data>/<location>/<checksum> elements live here).
_REPOMD_NS = "http://linux.duke.edu/metadata/repo"
# The repodata streams the build-time solve needs: primary (provides/requires) + filelists
# (file-path provides, for file-dep BuildRequires). comps/other/updateinfo and the db/zck
# encodings aren't needed at build time and are dropped so libdnf5 never tries to fetch them.
_BUILD_STREAMS = ("primary", "filelists")


def snapshot_repodata(rid: str, baseurl: str) -> dict:
    """Pin one repo's build-time repodata.

    Fetches repomd.xml, drops every <data> record except the build streams (so the served
    repomd lists only what we pin — libdnf5 then never tries to download the streams we
    skip), and records each kept stream as a sha-pinned download stanza (sha256 from
    repomd's own checksum). Returns {id, baseurl, repomd: <filtered xml>, streams: [{out,
    url, sha256}]}: buck writes the filtered repomd + downloads the streams into a
    `repodata/` tree the plan resolves against; `baseurl` is where the resolved rpms are
    fetched from.
    """
    base = baseurl.rstrip("/") + "/"
    with urllib.request.urlopen(base + "repodata/repomd.xml") as f:
        repomd = f.read()

    ET.register_namespace("", _REPOMD_NS)  # keep the default (unprefixed) namespace on output
    root = ET.fromstring(repomd)

    streams = []
    for data in list(root.findall(f"{{{_REPOMD_NS}}}data")):
        if data.get("type") not in _BUILD_STREAMS:
            root.remove(data)  # drop other/updateinfo/comps/*_db/*_zck
            continue

        loc = data.find(f"{{{_REPOMD_NS}}}location")
        chk = data.find(f"{{{_REPOMD_NS}}}checksum[@type='sha256']")
        size = data.find(f"{{{_REPOMD_NS}}}size")  # compressed size — lets http_file skip the HEAD probe
        href = loc.get("href") if loc is not None else None
        if href is None or chk is None or chk.text is None or size is None or size.text is None:
            raise SystemExit(f"{rid}: {data.get('type')} record lacks a location, sha256 checksum, or size")
        streams.append(
            {
                "out": href.rsplit("/", 1)[-1],
                "url": base + href,
                "sha256": chk.text,
                "size": int(size.text),
            }
        )

    if len(streams) != len(_BUILD_STREAMS):
        raise SystemExit(f"{rid}: repomd.xml missing one of {_BUILD_STREAMS}")

    filtered = ET.tostring(root, encoding="unicode", xml_declaration=True)
    return {"id": rid, "baseurl": base, "repomd": filtered, "streams": streams}


def resolve_one(name: str, distribution: dict) -> dict:
    """Resolve one distribution's engine + buildroot base names, and snapshot its buildroot repos' repodata.

    `engine` is either this distribution's own engine-root package set (a list — it
    provisions its own engine) or the name of the distribution whose engine builds it (a
    string). The buildroot itself is not resolved into a pool here: it's the pinned
    upstream repodata, resolved + fetched per build.
    """
    repos = _repos(distribution)
    with Pool(repos, distribution_id=name, arch=distribution.get("arch", "x86_64")) as pool:
        br = distribution["buildroot"]
        base = sorted(
            set(
                expand_groups(pool, br.get("buildroot_groups", [])) + br.get("buildroot_packages", []),
            )
        )
        print(f"{name}: buildroot base = {len(base)} packages", file=sys.stderr)

        engine_spec = distribution["engine"]
        if isinstance(engine_spec, list):
            # Self-hosting: `engine` is this distribution's engine-root package set.
            if not engine_spec:
                raise SystemExit(f"{name}: `engine` is an empty package list")
            erpms = resolve_closure(pool, engine_spec)
            print(f"{name}: engine root = {len(erpms)} packages", file=sys.stderr)
            engine: list[Entry] | str = entries(pool, erpms)
        else:
            # Consumer: `engine` names the distribution whose engine builds this one. The
            # reference is validated structurally by buck (the distribution points at
            # `:engine.<name>.root`, which must exist), so no cross-distribution check here.
            engine = engine_spec

    print(f"{name}: snapshotting repodata for {len(repos)} repo(s)…", file=sys.stderr)
    return {
        "package_format": "rpm",  # this is the rpm resolver; deb/etc. emit their own
        "engine": engine,
        "buildroot": base,
        "buildroot_repos": [snapshot_repodata(rid, baseurl) for rid, baseurl in repos],
    }


def cmd_resolve(args: argparse.Namespace) -> None:
    catalog_dir = Path(args.catalog_dir) if args.catalog_dir else None
    manifest_path = Path(args.manifest) if args.manifest else None
    if manifest_path is None:
        if catalog_dir is None:
            raise SystemExit("resolve: pass --catalog-dir or --manifest")
        manifest_path = catalog_dir / "manifest.toml"
    out_dir = Path(args.out_dir) if args.out_dir else catalog_dir
    if out_dir is None:
        raise SystemExit("resolve: pass --catalog-dir or --out-dir")

    with manifest_path.open("rb") as f:
        manifest = tomllib.load(f)
    distributions = manifest["distribution"]
    if args.distribution not in distributions:
        raise SystemExit(f"resolve: distribution {args.distribution!r} not in {manifest_path}")

    dist = resolve_one(args.distribution, distributions[args.distribution])
    # Explicit encoding/newline so the fragment is byte-identical regardless of host
    # locale or platform newline conventions.
    fragment = out_dir / f"{args.distribution}.json"
    fragment.write_text(render_fragment(dist), encoding="utf-8", newline="\n")
    print(f"wrote {fragment}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="buckify-rpm")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("resolve", help="resolve one distribution into its <name>.json fragment")
    s.add_argument("--distribution", required=True, help="the distribution (manifest key) to resolve")
    s.add_argument(
        "--catalog-dir",
        help="catalog dir; defaults --manifest to <dir>/manifest.toml and writes <dir>/<distribution>.json",
    )
    s.add_argument("--manifest", help="catalog manifest TOML (default: <catalog-dir>/manifest.toml)")
    s.add_argument("--out-dir", help="dir to write <distribution>.json into (default: <catalog-dir>)")
    s.set_defaults(func=cmd_resolve)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
