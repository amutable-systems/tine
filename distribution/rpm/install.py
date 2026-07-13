#!/usr/bin/python3
"""Install an exact RPM set into a fresh root or persisted overlay delta.

The same driver bootstraps engines, assembles buildroots, and extends image
layers. It parks the rpmdb and removes nondeterministic bookkeeping before capture.
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

import libdnf5
import rootfs

DBPATH = "usr/lib/sysimage/rpm"

# A fixed install path avoids embedding Buck hashes and supports scriptlet chroots.
BUILDROOT = "/buildroot"


def install(rpms_dir: Path, installroot: Path, cachedir: Path, *, system: bool = False) -> None:
    base = libdnf5.base.Base()
    cfg = base.get_config()
    cfg.installroot = str(installroot)
    cfg.cachedir = str(cachedir)
    cfg.install_weak_deps = False
    # Package digests are already pinned; disable both checks used by Transaction.run().
    cfg.pkg_gpgcheck = False
    cfg.localpkg_gpgcheck = False
    base.setup()

    sack = base.get_repo_sack()
    paths = [str(p) for p in sorted(rpms_dir.glob("*.rpm"))]
    sack.add_cmdline_packages(paths)
    if system:
        # Let installed packages satisfy dependencies for incremental installs.
        sack.load_repos(libdnf5.repo.Repo.Type_SYSTEM)

    # The directory is the exact set; scope the query to its command-line packages.
    query = libdnf5.rpm.PackageQuery(base)
    if system:
        query.filter_repo_id(["@commandline"])
    goal = libdnf5.base.Goal(base)
    # SWIG exposes iteration at runtime but not in its type information.
    packages: list[libdnf5.rpm.Package] = list(query)  # ty: ignore
    for pkg in packages:
        goal.add_rpm_install(pkg)
    tx = goal.resolve()

    problems = tx.get_resolve_logs_as_strings()
    if problems:
        raise SystemExit("install resolution failed:\n  " + "\n  ".join(problems))

    n = len(tx.get_transaction_packages())
    print(f"installing {n} rpms into {installroot}", file=sys.stderr)
    tx.set_description("buckify-rpm install")
    if tx.run() != libdnf5.base.Transaction.TransactionRunResult_SUCCESS:
        raise SystemExit("transaction failed:\n  " + "\n  ".join(tx.get_transaction_problems()))


def parkdb(installroot: Path) -> None:
    """Checkpoint, compact, and remove side files for a byte-stable rpmdb."""
    dbdir = installroot / DBPATH
    db = dbdir / "rpmdb.sqlite"
    if not db.exists():
        # sqlite3.connect would silently create a bogus empty database.
        raise SystemExit(f"no rpmdb at {db}; the install did not populate it")
    con = sqlite3.connect(db, isolation_level=None)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("VACUUM")
    finally:
        con.close()
    # rpm recreates these files; retaining them would destabilize the content key.
    for junk in ("rpmdb.sqlite-wal", "rpmdb.sqlite-shm", ".rpm.lock"):
        (dbdir / junk).unlink(missing_ok=True)


def scrub(installroot: Path) -> None:
    """Remove nondeterministic tooling bookkeeping."""
    shutil.rmtree(installroot / "usr/lib/sysimage/libdnf5", ignore_errors=True)
    (installroot / "var/cache/ldconfig/aux-cache").unlink(missing_ok=True)


def resolv_symlink(installroot: Path) -> None:
    """Create the Fedora-style mountpoint for the sandbox's resolver bind."""
    resolv = installroot / "etc/resolv.conf"
    resolv.unlink(missing_ok=True)
    resolv.symlink_to("../run/systemd/resolve/stub-resolv.conf")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="install")
    p.add_argument("--packages-dir", required=True, help="the complete set of packages to install")
    p.add_argument("--target", required=True, help="output root dir; bound at /buildroot to install into")
    p.add_argument(
        "--lower",
        action="append",
        default=[],
        help="existing-tree delta (bottom..top); --target becomes the install's overlay upper",
    )
    p.add_argument("--work", help="throwaway overlay workdir (required with --lower)")
    p.add_argument("--cachedir", default="/var/tmp/install-cache")
    p.add_argument("--resolv-symlink", action="store_true", help="add /etc/resolv.conf (engine root only)")
    p.add_argument("--no-parkdb", action="store_true")
    args = p.parse_args(argv)

    # libdnf5 needs absolute paths, and bind sources must exist.
    target = Path(args.target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    packages_dir = Path(args.packages_dir).resolve()

    incremental = bool(args.lower)
    if incremental:
        if args.work is None:
            raise SystemExit("--lower needs a --work overlay workdir")
        root = rootfs.rootfs(
            BUILDROOT, lowers=args.lower, upperdir=target, workdir=Path(args.work).resolve(), apivfs=True
        )
    else:
        root = rootfs.rootfs(BUILDROOT, bind=target, apivfs=True)

    with root:
        installroot = Path(BUILDROOT)

        # Prevent systemd scriptlets from generating a random machine ID.
        etc = installroot / "etc"
        etc.mkdir(exist_ok=True)
        (etc / "machine-id").write_text("uninitialized\n")

        install(packages_dir, installroot, Path(args.cachedir), system=incremental)
        if not args.no_parkdb:
            parkdb(installroot)
        scrub(installroot)
        if args.resolv_symlink:
            resolv_symlink(installroot)

    if not incremental:
        # Capture after teardown so the walk cannot descend into apivfs mounts.
        rootfs.capture(target)


if __name__ == "__main__":
    main()
