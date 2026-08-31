"""Run one native package installation against the root its spec names.

Every package system's installer answers the same request: a directory holding the exact package
closure, and either a fresh root to fill, a delta to persist over a lower stack, or a root the
caller has already mounted. Which of those it is, and how the root is mounted and captured, is
the same work whichever system fills it, so it happens here and the driver is left with the
transaction itself.
"""

import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

import specs

import rootfs

# A fixed install path avoids embedding Buck hashes and supports scriptlet chroots.
BUILDROOT = "/buildroot"

MINIMAL_NSSWITCH = """\
passwd: files
group: files
shadow: files
hosts: files dns
"""


class InstallSpec(TypedDict):
    """The request `package/install.bzl`, `box/build.bzl` and an image layer all write."""

    arch: str
    packages_dir: str
    # Either an output root, bound at /buildroot to install into, or a root the caller mounted.
    target: str | None
    installroot: str | None
    lower: list[str]
    work: str | None
    box_config: bool
    langs: list[str]
    docs: bool


@contextmanager
def fresh_machine_id(installroot: Path) -> Iterator[None]:
    """Leave a fresh root the marker systemd initializes on first boot, and an existing one be."""
    etc = installroot / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    machine_id = etc / "machine-id"
    fresh = not machine_id.exists()
    if fresh:
        machine_id.write_text("uninitialized\n")
    yield
    if fresh:
        # Packages may replace the marker during the transaction.
        machine_id.write_text("uninitialized\n")


def _configure_box(installroot: Path) -> None:
    """Materialize configuration needed before the box is ever booted."""
    factory_nsswitch = installroot / "usr/share/factory/etc/nsswitch.conf"
    nsswitch = installroot / "etc/nsswitch.conf"
    if factory_nsswitch.is_file():
        shutil.copy2(factory_nsswitch, nsswitch)
    elif not nsswitch.is_file():
        # Minimal boxes need not install systemd's factory configuration.
        nsswitch.write_text(MINIMAL_NSSWITCH)

    # Create the mountpoint for the sandbox's resolver bind.
    resolv = installroot / "etc/resolv.conf"
    resolv.unlink(missing_ok=True)
    resolv.symlink_to("../run/systemd/resolve/stub-resolv.conf")


def _normalize(installroot: Path, spec: InstallSpec) -> None:
    """Settle what an install leaves behind whichever package system performed it.

    A driver parks its own database, because only it knows what its package manager wrote. What is
    here is what no package system owns: scriptlets that any of them run, and the configuration a
    box needs before it is first entered.
    """
    # ldconfig's auxiliary cache stores inode numbers and mtimes; ld.so.cache itself does not.
    (installroot / "var/cache/ldconfig/aux-cache").unlink(missing_ok=True)
    if spec["box_config"]:
        _configure_box(installroot)


def run(
    prog: str,
    install: Callable[[Path, Path, InstallSpec, bool], None],
    argv: list[str] | None = None,
) -> None:
    """Mount the root this invocation names and hand it to `install`.

    The callback receives the closure directory, the root to install into, the spec, and whether
    it is layering over packages a lower stack already carries.
    """
    spec = specs.parse(InstallSpec, prog, argv)
    # We already build the kernel/initrd in the ESP. Prevent systemd's `kernel-install` (called via
    # package install scripts) from building and writing its own (dead weight and waste of time).
    os.environ["KERNEL_INSTALL_BYPASS"] = "1"

    # An installer resolves its own paths against the root, so give it no relative ones.
    packages_dir = Path(spec["packages_dir"]).absolute()
    layered = bool(spec["lower"])

    if spec["installroot"] is not None:
        if spec["target"] is not None or layered or spec["work"] is not None:
            raise SystemExit(f"{prog}: installroot excludes target, lower, and work")
        installroot = Path(spec["installroot"]).absolute()
        install(packages_dir, installroot, spec, layered)
        _normalize(installroot, spec)
        return

    if spec["target"] is None:
        raise SystemExit(f"{prog}: one of target and installroot is required")

    # Bind sources must exist.
    target = Path(spec["target"]).absolute()
    target.mkdir(parents=True, exist_ok=True)

    if layered:
        if spec["work"] is None:
            raise SystemExit(f"{prog}: lower needs a work overlay directory")
        root = rootfs.rootfs(
            BUILDROOT,
            lowers=spec["lower"],
            upperdir=target,
            workdir=Path(spec["work"]).absolute(),
            apivfs=True,
        )
    else:
        root = rootfs.rootfs(BUILDROOT, bind=target, capture_bind=True, apivfs=True)

    with root:
        install(packages_dir, Path(BUILDROOT), spec, layered)
        _normalize(Path(BUILDROOT), spec)
