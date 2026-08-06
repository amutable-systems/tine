"""Run one native package installation against the root its spec names.

Every package system's installer answers the same request: a directory holding the exact package
closure, and either a fresh root to fill, a delta to persist over a lower stack, or a root the
caller has already mounted. Which of those it is, and how the root is mounted and captured, is
the same work whichever system fills it, so it happens here and the driver is left with the
transaction itself.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

import specs

import rootfs

# A fixed install path avoids embedding Buck hashes and supports scriptlet chroots.
BUILDROOT = "/buildroot"


class InstallSpec(TypedDict):
    """The request `package/install.bzl`, `engine/build.bzl` and an image layer all write."""

    arch: str
    packages_dir: str
    # Either an output root, bound at /buildroot to install into, or a root the caller mounted.
    target: str | None
    installroot: str | None
    lower: list[str]
    work: str | None
    engine_config: bool
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


def run(
    prog: str,
    install: Callable[[Path, Path, InstallSpec, bool], None],
    argv: list[str] | None = None,
) -> None:
    """Mount the root this invocation names and hand it to `install`.

    The callback receives the closure directory, the root to install into, the spec, and whether
    it is layering over packages a lower stack already carries.
    """
    spec: InstallSpec = specs.parse(prog, argv)
    # An installer resolves its own paths against the root, so give it no relative ones.
    packages_dir = Path(spec["packages_dir"]).absolute()
    layered = bool(spec["lower"])

    if spec["installroot"] is not None:
        if spec["target"] is not None or layered or spec["work"] is not None:
            raise SystemExit(f"{prog}: installroot excludes target, lower, and work")
        install(packages_dir, Path(spec["installroot"]).absolute(), spec, layered)
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
        root = rootfs.rootfs(BUILDROOT, bind=target, apivfs=True)

    with root:
        install(packages_dir, Path(BUILDROOT), spec, layered)

    if not layered:
        # Capture after teardown so the walk cannot descend into apivfs mounts.
        rootfs.capture(target)
