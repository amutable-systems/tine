# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Deterministic tar archives.

Entries are written in sorted order with ownership normalized to uid/gid 0 and mtimes clamped to
the build's epoch, so the same tree packs to the same bytes on any machine.
"""

import os
import tarfile
from collections.abc import Callable
from pathlib import Path


def _xattrs(path: Path) -> dict[str, str]:
    """Encode Linux xattrs using the convention understood by GNU tar and star."""
    headers = {}
    for name in sorted(os.listxattr(path, follow_symlinks=False)):
        if name.startswith(("user.overlay.", "trusted.overlay.")):
            continue
        value = os.getxattr(path, name, follow_symlinks=False)
        headers["SCHILY.xattr." + name] = value.decode("utf-8", "surrogateescape")
    return headers


def _reproducible(path: Path, epoch: int) -> Callable[[tarfile.TarInfo], tarfile.TarInfo]:
    """Return a tar filter that normalizes ownership, mtimes, and extended attributes."""

    def reset(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = min(int(info.mtime), epoch)
        info.pax_headers = dict(info.pax_headers) | _xattrs(path)
        return info

    return reset


def pack_tree(tree: Path, out: Path, epoch: int) -> int:
    """Pack a whole tree into `out`."""
    tree = Path(tree)
    count = 0
    # Explicit sorted entries keep archive order stable.
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(tree.rglob("*")):
            archive.add(
                path,
                arcname="./" + str(path.relative_to(tree)),
                recursive=False,
                filter=_reproducible(path, epoch),
            )
            count += 1
    return count
