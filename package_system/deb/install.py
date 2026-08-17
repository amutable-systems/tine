#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Install an exact package set into a fresh, layered, or already-mounted root.

The same driver bootstraps boxes, assembles buildroots, and extends image layers. dpkg applies
the transaction, so maintainer scripts run as they would on a real system; the planner has
already chosen the exact set, and dpkg checks the closure again as it configures it.

A fresh root is laid down before dpkg is asked to do anything with it. dpkg has no way to defer a
pre-install script, so the first package whose `preinst` calls a shell would fail in a root that
has no shell yet, and a pre-dependency dpkg cannot see configured is one it refuses to unpack
behind. Extracting the closure first, without scripts, is what debootstrap does for the same
reason: it puts the tools on disk that the scripts dpkg then runs properly go looking for.
"""

import fnmatch
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import arches
import debfile
import util

import installer

# What documentation costs an image.
DOC_PATHS = (
    "/usr/share/doc/*",
    "/usr/share/man/*",
    "/usr/share/groff/*",
    "/usr/share/info/*",
    "/usr/share/gtk-doc/*",
)
COPYRIGHT = "/usr/share/doc/*/copyright"

# Debian's policy is to start a daemon when its package is installed. There is no init to start
# one against here, and an image is not its own runtime, so deny every invocation.
# See https://people.debian.org/~hmh/invokerc.d-policyrc.d-specification.txt.
POLICY_RC_D = "#!/bin/sh\nexit 101\n"


def _paths(docs: bool, langs: list[str]) -> list[tuple[bool, str]]:
    """Express what rpm's `nodocs` and `_install_langs` do, as dpkg's own ordered path rules.

    A later rule wins, which is how the licenses are carved back out of the documentation and the
    kept languages out of the locales.
    """
    rules = []
    if not docs:
        rules += [(False, path) for path in DOC_PATHS]
        # The licenses every package ships stay installed; only the documentation goes.
        rules.append((True, COPYRIGHT))
    if langs:
        rules.append((False, "/usr/share/locale/*"))
        rules += [(True, f"/usr/share/locale/{lang}/*") for lang in langs]
    return rules


def _dpkg_paths(rules: list[tuple[bool, str]]) -> list[str]:
    """The rules as dpkg's own arguments."""
    return [f"--path-{'include' if include else 'exclude'}={path}" for include, path in rules]


def _keeps(rules: list[tuple[bool, str]]) -> Callable[[str], bool]:
    """The same rules as a predicate, for the pass dpkg does not perform.

    dpkg's `--path-exclude` only skips extraction: it never removes a path already on disk, and it
    records nothing about one, so a file the pre-extraction laid down would both survive the
    exclusion and be absent from the package's own file list.
    """

    def keeps(path: str) -> bool:
        keep = True
        for include, pattern in rules:
            if fnmatch.fnmatchcase(path, pattern):
                keep = include
        return keep

    return keeps


def is_fresh(installroot: Path) -> bool:
    """Whether nothing has been installed into this root yet."""
    return not (installroot / debfile.ADMINDIR / "status").is_file()


def prepare(installroot: Path) -> None:
    """Create the bookkeeping dpkg expects to find.

    Only ever called for a transaction that installs something: `touch` is a setattr, and overlayfs
    copies a lower file up on one, so doing this for an empty closure would materialize the lower
    stack's whole database into a layer that installed nothing.
    """
    admin = installroot / debfile.ADMINDIR
    for directory in ("info", "triggers", "updates"):
        (admin / directory).mkdir(parents=True, exist_ok=True)
    for name in ("status", "available"):
        (admin / name).touch(exist_ok=True)


def merge_usr(installroot: Path, arch: str) -> None:
    """Point the compatibility directories at `/usr` before anything is unpacked into the root."""
    for name in arches.merged_usr(arch):
        (installroot / "usr" / name).mkdir(parents=True, exist_ok=True)
        link = installroot / name
        if not link.is_symlink() and not link.exists():
            link.symlink_to(f"usr/{name}")


def _replace(path: Path, content: bytes, mode: int) -> None:
    """Write a file that is this path and not whatever a symlink there points at."""
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.write(descriptor, content)
    finally:
        os.close(descriptor)
    path.chmod(mode)


@contextmanager
def denied_daemons(installroot: Path) -> Iterator[None]:
    """Deny daemon startup for the length of the transaction, leaving nothing behind.

    The file is not shipped by any package and belongs to the administrator, so an image must not
    end up carrying the one this install needed.
    """
    policy = installroot / "usr/sbin/policy-rc.d"
    created = not policy.parent.is_dir()
    policy.parent.mkdir(parents=True, exist_ok=True)
    # Only a regular file here is one the image ships. The closure is laid down before this runs,
    # so a package is free to have planted a symlink pointing out of the root, and writing through
    # it would edit the build host rather than the image.
    kept = None
    if not policy.is_symlink() and policy.is_file():
        kept = (policy.read_bytes(), stat.S_IMODE(policy.stat().st_mode))
    try:
        # Inside the try: `_replace` unlinks before it writes, so a failure partway leaves the
        # administrator's file gone, and only the restore below can put it back.
        _replace(policy, POLICY_RC_D.encode(), 0o755)
        yield
    finally:
        policy.unlink(missing_ok=True)
        if kept is not None:
            _replace(policy, *kept)
        elif created and not any(policy.parent.iterdir()):
            # The transaction may have installed into a directory this created; leaving one behind
            # is better than failing an install that already succeeded.
            policy.parent.rmdir()


def environment() -> dict[str, str]:
    """The environment a maintainer script is run under, with nobody to answer a prompt."""
    return {
        **os.environ,
        "DEBIAN_FRONTEND": "noninteractive",
        "DEBCONF_NONINTERACTIVE_SEEN": "true",
        # Debian packages ship initramfs hooks that would build one into the image; tine builds
        # its own initrd and ships a usr-only disk, so all of that work would be discarded.
        "INITRD": "No",
    }


def _run(command: list[str], what: str) -> None:
    if subprocess.run(command, env=environment()).returncode != 0:
        util.fail(f"dpkg {what} failed")


def install(
    packages_dir: Path,
    installroot: Path,
    *,
    arch: str,
    langs: list[str] | None = None,
    docs: bool = True,
) -> None:
    packages = sorted(path for path in packages_dir.iterdir() if path.suffix == ".deb")
    rules = _paths(docs, langs or [])
    fresh = is_fresh(installroot)
    if not packages:
        # A layer whose request a lower one already satisfies resolves to nothing, which is not an
        # error: the root it would have installed into is already the root that was wanted. A root
        # with no database underneath it would instead be left empty, which is not what was asked.
        if fresh:
            util.fail("install: nothing to install, and nothing installed underneath")
        print("nothing to install", file=sys.stderr)
        return

    prepare(installroot)

    if fresh:
        merge_usr(installroot, arch)
        keeps = _keeps(rules)
        for package in packages:
            debfile.unpack(package, installroot, keeps)

    print(f"installing {len(packages)} packages into {installroot}", file=sys.stderr)
    root = [
        "dpkg",
        "--root", str(installroot),
        "--admindir", str(installroot / debfile.ADMINDIR),
        "--force-unsafe-io",
        # The transaction was resolved for one architecture; dpkg would take its own from the box
        # it is running in, which is not the same question.
        "--force-architecture",
        # dpkg verifies a signature whenever debsig-verify is anywhere on PATH, which is the box's
        # PATH rather than anything this build pins.
        "--no-debsig",
        *_dpkg_paths(rules),
    ]  # fmt: skip
    with denied_daemons(installroot):
        # Unpack the whole closure before configuring any of it: a package's `postinst` may call
        # a tool another package in the same transaction owns, and only dpkg knows the order that
        # settles. `--force-depends` covers the unpack alone, where nothing is configured yet.
        _run([*root, "--force-depends", "--unpack", *map(str, packages)], "unpack")
        _run([*root, "--configure", "--pending"], "configure")


def scrub(installroot: Path) -> None:
    """Remove the bookkeeping this package system leaves beside its database.

    dpkg's own transaction log is not among it: `--root` rebases where dpkg installs and keeps its
    database, but `log` comes from the box's `dpkg.cfg` and is taken as written, so that one lands
    in the box. What does land here is the locks, which describe a run rather than an image.
    """
    leftovers = (
        "var/log/alternatives.log",
        f"{debfile.ADMINDIR}/available",
        f"{debfile.ADMINDIR}/lock",
        f"{debfile.ADMINDIR}/lock-frontend",
        f"{debfile.ADMINDIR}/triggers/Lock",
    )
    for name in leftovers:
        (installroot / name).unlink(missing_ok=True)
    for leftover in (installroot / debfile.ADMINDIR).glob("*-old"):
        leftover.unlink()
    # dpkg journals an interrupted transaction here; a completed one leaves it empty.
    for update in (installroot / debfile.ADMINDIR / "updates").glob("*"):
        update.unlink()


def _install(packages_dir: Path, installroot: Path, spec: installer.InstallSpec, layered: bool) -> None:
    with installer.fresh_machine_id(installroot):
        install(
            packages_dir,
            installroot,
            arch=spec["arch"],
            langs=spec["langs"],
            docs=spec["docs"],
        )
    # Only what installed something has anything to scrub, and an unlink on an overlay is a
    # whiteout: scrubbing an empty layer would copy the lower stack's database up to delete it.
    if any(path.suffix == ".deb" for path in packages_dir.iterdir()):
        scrub(installroot)


def main(argv: list[str] | None = None) -> None:
    installer.run("install", _install, argv)


if __name__ == "__main__":
    main()
