"""install — install a complete set of rpms into a root.

One driver for both the engine-root bootstrap (Action B) and the buildroot
assembly (Action 3): the input dir is *exactly* the set to install — the seed
closure for the engine root, the plan's resolved closure for a buildroot — so
this just cmdline-installs every rpm in --packages-dir into --installroot via libdnf5
(no resolution against repos, no network), then parks the rpmdb + scrubs so the
root content-keys deterministically (SDE injected by the sandbox clamps the rpmdb).

Runs inside whichever root provides libdnf5: chroot1 (the payload-extracted seed)
when bootstrapping the engine, the engine root itself when assembling a buildroot.
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

import libdnf5
import rootfs

DBPATH = "usr/lib/sysimage/rpm"

# Install into a fixed path (bound to the real output tree) rather than the buck-out output
# path directly, so nothing captures the hashed path and libdnf5's scriptlet chroots find a
# working apivfs there. The writes land in the output tree through the bind.
BUILDROOT = "/buildroot"


def install(rpms_dir: Path, installroot: Path, cachedir: Path) -> None:
    base = libdnf5.base.Base()
    cfg = base.get_config()
    cfg.installroot = str(installroot)
    cfg.cachedir = str(cachedir)
    cfg.install_weak_deps = False
    cfg.gpgcheck = False
    base.setup()

    sack = base.get_repo_sack()
    paths = [str(p) for p in sorted(rpms_dir.glob("*.rpm"))]
    sack.add_cmdline_packages(paths)

    # Install everything: the dir is already the exact set, so there's nothing to
    # resolve against repos. With no repos loaded, a bare query is exactly the
    # cmdline packages we just added.
    goal = libdnf5.base.Goal(base)
    # SWIG makes the query iterable at runtime but ty can't see __iter__; the
    # annotation restores the element type so the loop body type-checks.
    packages: list[libdnf5.rpm.Package] = list(libdnf5.rpm.PackageQuery(base))  # ty: ignore
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
    """Park the installed rpmdb for byte-stability.

    Replicates rpm's own `rpmdb --parkdb` (not yet in a released rpm) directly on
    the sqlite db: the sequence rpm runs on close when RPMDB_FLAG_PARK is set
    (rpm lib/backend/sqlite.cc) — checkpoint+truncate the WAL, switch to a
    rollback journal so the -wal/-shm side files are torn down, then VACUUM to
    compact into a deterministic page layout. With the seed-pinned sqlite this
    makes the rpmdb byte-stable, so the root content-keys deterministically.
    """
    dbdir = installroot / DBPATH
    db = dbdir / "rpmdb.sqlite"
    if not db.exists():
        # sqlite3.connect would silently create an empty db; a missing rpmdb means
        # the install didn't land, so fail instead of parking a bogus empty file.
        raise SystemExit(f"no rpmdb at {db}; the install did not populate it")
    con = sqlite3.connect(db, isolation_level=None)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("VACUUM")
    finally:
        con.close()
    # Drop side files + the empty lock so the parked tree is a single db file
    # (rpm recreates them on next open); their presence/noise would defeat early
    # cutoff on the root's content key.
    for junk in ("rpmdb.sqlite-wal", "rpmdb.sqlite-shm", ".rpm.lock"):
        (dbdir / junk).unlink(missing_ok=True)


def scrub(installroot: Path) -> None:
    """Remove non-deterministic tooling bookkeeping so the whole tree content-keys
    deterministically (not just the rpmdb).
    """
    shutil.rmtree(installroot / "usr/lib/sysimage/libdnf5", ignore_errors=True)
    (installroot / "var/cache/ldconfig/aux-cache").unlink(missing_ok=True)


def resolv_symlink(installroot: Path) -> None:
    """A resolv.conf symlink (Fedora-style, into /run) so a networked tool can
    nofollow-bind the host's over it — /etc is ro in the sandbox, so the mountpoint
    must already exist (and be a symlink).
    """
    resolv = installroot / "etc/resolv.conf"
    resolv.unlink(missing_ok=True)
    resolv.symlink_to("../run/systemd/resolve/stub-resolv.conf")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="install")
    p.add_argument("--packages-dir", required=True, help="the complete set of packages to install")
    p.add_argument("--target", required=True, help="output root dir; bound at /buildroot to install into")
    p.add_argument("--cachedir", default="/var/tmp/install-cache")
    p.add_argument("--resolv-symlink", action="store_true", help="add /etc/resolv.conf (engine root only)")
    p.add_argument("--no-parkdb", action="store_true")
    args = p.parse_args(argv)

    # Paths are project-relative (buck-out) and resolved against the bound cwd; make them
    # absolute for libdnf5/rpm and create the output root (the bind's source must exist).
    target = Path(args.target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    packages_dir = Path(args.packages_dir).resolve()

    with rootfs.rootfs(BUILDROOT, bind=target, apivfs=True):
        installroot = Path(BUILDROOT)
        install(packages_dir, installroot, Path(args.cachedir))
        if not args.no_parkdb:
            parkdb(installroot)
        scrub(installroot)
        if args.resolv_symlink:
            resolv_symlink(installroot)


if __name__ == "__main__":
    main()
