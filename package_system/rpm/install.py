#!/usr/bin/python3
"""Install an exact RPM set into a fresh, layered, or already-mounted root.

The same driver bootstraps engines, assembles buildroots, and extends image
layers. It parks the rpmdb and removes nondeterministic bookkeeping before capture.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import TypedDict

import libdnf5
import specs

import rootfs

DBPATH = "usr/lib/sysimage/rpm"

# A fixed install path avoids embedding Buck hashes and supports scriptlet chroots.
BUILDROOT = "/buildroot"

CACHEDIR = Path("/var/tmp/install-cache")


class Spec(TypedDict):
    packages_dir: str
    # Either an output root, bound at /buildroot to install into, or a root the caller mounted.
    target: str | None
    installroot: str | None
    lower: list[str]
    work: str | None
    engine_config: bool
    langs: list[str]
    docs: bool


MINIMAL_NSSWITCH = """\
passwd: files
group: files
shadow: files
hosts: files dns
"""


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
    """Remove nondeterministic tooling bookkeeping."""
    shutil.rmtree(installroot / "usr/lib/sysimage/libdnf5", ignore_errors=True)
    (installroot / "var/cache/ldconfig/aux-cache").unlink(missing_ok=True)


def configure_engine(installroot: Path) -> None:
    """Materialize configuration needed before the engine is ever booted."""
    factory_nsswitch = installroot / "usr/share/factory/etc/nsswitch.conf"
    nsswitch = installroot / "etc/nsswitch.conf"
    if factory_nsswitch.is_file():
        shutil.copy2(factory_nsswitch, nsswitch)
    elif not nsswitch.is_file():
        # Minimal engines need not install systemd's factory configuration.
        nsswitch.write_text(MINIMAL_NSSWITCH)

    # Create the Fedora-style mountpoint for the sandbox's resolver bind.
    resolv = installroot / "etc/resolv.conf"
    resolv.unlink(missing_ok=True)
    resolv.symlink_to("../run/systemd/resolve/stub-resolv.conf")


def install_into_root(
    packages_dir: Path,
    installroot: Path,
    *,
    system: bool,
    engine_config: bool,
    langs: list[str] | None = None,
    docs: bool = True,
) -> None:
    # Leave a fresh root for systemd to initialize on first boot without resetting existing images.
    etc = installroot / "etc"
    etc.mkdir(exist_ok=True)
    machine_id = etc / "machine-id"
    initialize_machine_id = not machine_id.exists()
    if initialize_machine_id:
        machine_id.write_text("uninitialized\n")

    install(packages_dir, installroot, CACHEDIR, system=system, langs=langs, docs=docs)
    if initialize_machine_id:
        # Packages may replace the marker during the transaction.
        machine_id.write_text("uninitialized\n")
    parkdb(installroot)
    scrub(installroot)
    if engine_config:
        configure_engine(installroot)


def main(argv: list[str] | None = None) -> None:
    spec: Spec = specs.parse("install", argv)
    packages_dir = Path(spec["packages_dir"]).absolute()

    if spec["installroot"] is not None:
        if spec["target"] is not None or spec["lower"] or spec["work"] is not None:
            raise SystemExit("install: installroot excludes target, lower, and work")
        installroot = Path(spec["installroot"]).absolute()
        install_into_root(
            packages_dir,
            installroot,
            system=(installroot / DBPATH / "rpmdb.sqlite").exists(),
            engine_config=spec["engine_config"],
            langs=spec["langs"],
            docs=spec["docs"],
        )
        return

    if spec["target"] is None:
        raise SystemExit("install: one of target and installroot is required")

    # libdnf5 needs absolute paths, and bind sources must exist.
    target = Path(spec["target"]).absolute()
    target.mkdir(parents=True, exist_ok=True)

    incremental = bool(spec["lower"])
    if incremental:
        if spec["work"] is None:
            raise SystemExit("install: lower needs a work overlay directory")
        root = rootfs.rootfs(
            BUILDROOT,
            lowers=spec["lower"],
            upperdir=target,
            workdir=Path(spec["work"]).absolute(),
            apivfs=True,
        )
    else:
        root = rootfs.rootfs(BUILDROOT, bind=target, apivfs=True)

    with root:
        install_into_root(
            packages_dir,
            Path(BUILDROOT),
            system=incremental,
            engine_config=spec["engine_config"],
            langs=spec["langs"],
            docs=spec["docs"],
        )

    if not incremental:
        # Capture after teardown so the walk cannot descend into apivfs mounts.
        rootfs.capture(target)


if __name__ == "__main__":
    main()
