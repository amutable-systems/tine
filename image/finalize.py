# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Finalizing an image: the generators a layer persists, and the preparation a terminal applies.

A generator turns installed package content into the derived state a system boots from, such as
the module and hardware databases or the locale archive. Each is one image operation of its own,
and what it writes is an ordinary file, so a layer captures it. Terminal preparation is the other
half: the authored tmpfiles snippets a driver applies to the stack it just mounted, which no
layer can hold.
"""

import contextlib
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TypedDict

from util import fail

import rootfs


class ImageSpec(TypedDict):
    """The half of a terminal driver's spec that names the image itself."""

    lower: list[str]
    tmpfiles: list[str]


# The directories whose whole point is a mode, so a layer cannot carry them: sticky and
# world-writable, which is exactly what Buck cannot store and the tar filter strips.
_WORLD_WRITABLE = ("tmp", "var/tmp")


def world_writable(tree: Path) -> None:
    """Restore the mode on the temporary directories, which nothing upstream of here can keep.

    Buck stores no mode beyond the executable bit, so a package's `0o1777` is gone by the time a
    layer is a build artifact however faithfully it was extracted. Here is the last point before
    the image is written, and the only one where the mode survives into it.
    """
    for name in _WORLD_WRITABLE:
        path = tree / name
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o1777)


@contextlib.contextmanager
def image(
    spec: ImageSpec,
    *,
    program: str,
    lowers: Sequence[str | Path] | None = None,
    binds: Sequence[tuple[str | Path, str | Path]] | None = None,
) -> Iterator[Path]:
    """Mount the image stack the spec names and yield the finalized tree."""
    with rootfs.rootfs(
        "/buildroot", lowers=spec["lower"] if lowers is None else lowers, binds=binds
    ) as tree:
        # Before the snippets, so an image that states a mode of its own still wins.
        world_writable(tree)
        apply_tmpfiles(tree, spec["tmpfiles"], program=program)
        yield tree


def apply_tmpfiles(tree: Path, snippets: list[str], *, program: str) -> None:
    """Apply authored tmpfiles configuration beneath `tree`."""
    if not snippets:
        return
    tmpfiles = shutil.which("systemd-tmpfiles")
    if tmpfiles is None:
        fail(f"{program}: systemd-tmpfiles is required to finalize this image")

    config = "".join(snippet if snippet.endswith("\n") else snippet + "\n" for snippet in snippets)
    subprocess.run(
        [tmpfiles, "--create", f"--root={tree}", "-"],
        input=config,
        text=True,
        check=True,
    )


def _box_tool(name: str, what: str) -> str:
    path = shutil.which(name)
    if path is None:
        fail(f"{what}: this box carries no {name}")
    return path


def _image_tool(name: str, what: str) -> str:
    """Find a tool inside the already-entered image, which is the only place it can come from."""
    path = shutil.which(name)
    if path is None:
        fail(f"{what}: the image itself has to carry {name}, and does not")
    return path


def _run(what: str, cmd: list[str]) -> None:
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        fail(f"{what}: `{' '.join(cmd)}` failed (rc={rc})")


def kernel_versions(tree: Path) -> list[str]:
    """The module directories holding a kernel, which are the ones depmod has work to do in."""
    modules = tree / "usr/lib/modules"
    if not modules.is_dir():
        return []
    # A directory holding nothing but `updates` belongs to a kernel this image does not install.
    return sorted(
        entry.name
        for entry in modules.iterdir()
        if entry.is_dir() and {child.name for child in entry.iterdir()} - {"updates"}
    )


def depmod(tree: Path) -> None:
    """Rebuild the module indexes modprobe and udev read, for every kernel the image installs."""
    versions = kernel_versions(tree)
    if not versions:
        return
    # depmod reads its search-order configuration from absolute paths that `--basedir` does not
    # move, and writes indexes its own kmod defines, so this is the image's depmod and no other.
    with rootfs.chroot(tree):
        tool = _image_tool("depmod", "depmod")
        for version in versions:
            print(f"depmod: {version}", file=sys.stderr)
            _run("depmod", [tool, "--all", version])


def hwdb_command(tool: str, tree: Path, *, usr: bool, strict: bool) -> list[str]:
    """The systemd-hwdb invocation building the binary database from the image's hwdb.d."""
    return [
        tool,
        f"--root={tree}",
        *(["--usr"] if usr else []),
        *(["--strict"] if strict else []),
        "update",
    ]


def hwdb(tree: Path, *, usr: bool, strict: bool) -> None:
    """Compile the image's hardware database, which udev reads in binary form and never sources."""
    if not any((tree / source).is_dir() for source in ("usr/lib/udev/hwdb.d", "etc/udev/hwdb.d")):
        return
    _run("hwdb", hwdb_command(_box_tool("systemd-hwdb", "hwdb"), tree, usr=usr, strict=strict))
    # A database in /etc would shadow the one in /usr the image now ships forever.
    if usr:
        (tree / "etc/udev/hwdb.bin").unlink(missing_ok=True)


def wants_locales(tree: Path) -> bool:
    """Whether the image asks for locales to be generated, in the shape locale-gen expects."""
    config = tree / "etc/locale.gen"
    if not config.is_file():
        return False
    return any(
        line.strip() and not line.strip().startswith("#")
        for line in config.read_text(encoding="utf-8").splitlines()
    )


def locale_gen(tree: Path) -> None:
    """Generate the locales /etc/locale.gen asks for, for the distributions that work that way."""
    if not wants_locales(tree):
        return
    # locale-gen is a distribution's own script over its own locale sources, not a shared tool.
    with rootfs.chroot(tree):
        _run("locale_gen", [_image_tool("locale-gen", "locale_gen")])
