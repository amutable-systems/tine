"""Shared helpers for the rpm drivers."""

import errno
import fcntl
import os
import shutil
from pathlib import Path

# Errnos meaning "clone/hardlink isn't supported here" — anything else (permissions, IO
# errors) is a real failure and propagates.
# Same as what `cp --reflink=auto` treats as "clone not supported"; ENOTTY is the kernel's generic
# "fd doesn't support this ioctl.
_CLONE_FALLBACK_ERRNOS = frozenset({errno.ENOTTY, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV})
_LINK_FALLBACK_ERRNOS = frozenset({errno.EMLINK, errno.EOPNOTSUPP, errno.EXDEV})


def clone_file(src: Path, dst: Path, allow_link: bool = False) -> None:
    """Copy src to dst without duplicating storage where the fs allows it.

    Like `cp --reflink=auto` with a hardlink fallback: CoW clone (btrfs/XFS), else
    hardlink, else plain copy. Hardlinks share the inode, so they're only safe between
    write-once outputs that live and die together; that needs an explicit allow_link=True
    opt-in, since through a hardlink a later write to either file reaches the other.
    src's permissions are not preserved (buck keys outputs on content, not mode).
    """
    with open(src, "rb") as s, open(dst, "wb") as d:
        try:
            fcntl.ioctl(d.fileno(), fcntl.FICLONE, s.fileno())
            return
        except OSError as e:
            if e.errno not in _CLONE_FALLBACK_ERRNOS:
                raise
    dst.unlink()  # drop the empty file the failed clone attempt created
    if allow_link:
        try:
            os.link(src, dst)
            return
        except OSError as e:
            if e.errno not in _LINK_FALLBACK_ERRNOS:
                raise
    shutil.copyfile(src, dst)
