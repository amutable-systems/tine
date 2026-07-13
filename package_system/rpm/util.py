"""Shared helpers for the rpm drivers."""

import errno
import fcntl
import os
import shutil
from pathlib import Path

# Fall back only when the filesystem does not support cloning or linking.
_CLONE_FALLBACK_ERRNOS = frozenset({errno.ENOTTY, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV})
_LINK_FALLBACK_ERRNOS = frozenset({errno.EMLINK, errno.EOPNOTSUPP, errno.EXDEV})


def clone_file(src: Path, dst: Path, allow_link: bool = False) -> None:
    """Clone, optionally hardlink, or copy src to dst while preserving its mode.

    Hardlinking requires an explicit opt-in because later writes affect both paths.
    """
    with open(src, "rb") as s, open(dst, "wb") as d:
        try:
            fcntl.ioctl(d.fileno(), fcntl.FICLONE, s.fileno())
        except OSError as e:
            if e.errno not in _CLONE_FALLBACK_ERRNOS:
                raise
        else:
            shutil.copymode(src, dst)
            return
    dst.unlink()  # drop the empty file the failed clone attempt created
    if allow_link:
        try:
            os.link(src, dst)  # shares src's mode via the inode
            return
        except OSError as e:
            if e.errno not in _LINK_FALLBACK_ERRNOS:
                raise
    shutil.copy(src, dst)  # copyfile + copymode
