#!/usr/bin/python3
"""Install an exact RPM set into a fresh, layered, or already-mounted root.

The same driver bootstraps boxes, assembles buildroots, and extends image
layers. It parks the rpmdb and removes nondeterministic bookkeeping before capture.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import libdnf5

import installer

DBPATH = "usr/lib/sysimage/rpm"

CACHEDIR = Path("/var/tmp/install-cache")


def limit_langs(langs: list[str]) -> None:
    """Restrict the `%lang()`-marked files rpm installs to the given languages.

    libdnf5 exposes neither a macro API nor an equivalent option, so this has to arrive through
    rpm's own macro path. XDG_CONFIG_HOME is the entry that keeps it inside this driver: writing
    /etc/rpm/macros.* as mkosi does would need a bind mount arranged by the action, outside the
    package system. The directory lives in the action's temporary space, which Buck clears
    before that action next runs.
    """
    config = Path(tempfile.mkdtemp(prefix="rpmconfig.")) / "rpm"
    config.mkdir()
    (config / "macros").write_text("%_install_langs {}\n".format(":".join(langs)))
    os.environ["XDG_CONFIG_HOME"] = str(config.parent)


def install(
    rpms_dir: Path,
    installroot: Path,
    cachedir: Path,
    *,
    system: bool = False,
    langs: list[str] | None = None,
    docs: bool = True,
) -> None:
    if langs:
        limit_langs(langs)
    base = libdnf5.base.Base()
    cfg = base.get_config()
    cfg.installroot = str(installroot)
    cfg.cachedir = str(cachedir)
    cfg.install_weak_deps = False
    if not docs:
        # This skips %doc only; the licenses packages ship stay installed.
        cfg.tsflags = ["nodocs"]
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
    packages: list[libdnf5.rpm.Package] = list(query)  # ty: ignore[invalid-argument-type]
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
    """Remove what this package system's transaction leaves behind."""
    shutil.rmtree(installroot / "usr/lib/sysimage/libdnf5", ignore_errors=True)
    # Fedora kernel package scriptlet writes it s own copy of the module symbol table into /boot;
    # we already have that in /usr/lib/modules
    for symvers in (installroot / "boot").glob("symvers-*.xz"):
        symvers.unlink()


def install_into_root(
    packages_dir: Path,
    installroot: Path,
    *,
    system: bool,
    langs: list[str] | None = None,
    docs: bool = True,
) -> None:
    with installer.fresh_machine_id(installroot):
        install(packages_dir, installroot, CACHEDIR, system=system, langs=langs, docs=docs)
    parkdb(installroot)
    scrub(installroot)


def _install(packages_dir: Path, installroot: Path, spec: installer.InstallSpec, layered: bool) -> None:
    install_into_root(
        packages_dir,
        installroot,
        # Installed packages satisfy dependencies for an incremental install; a root the caller
        # mounted says so by already carrying an rpmdb.
        system=layered or (installroot / DBPATH / "rpmdb.sqlite").exists(),
        langs=spec["langs"],
        docs=spec["docs"],
    )


def main(argv: list[str] | None = None) -> None:
    installer.run("install", _install, argv)


if __name__ == "__main__":
    main()
