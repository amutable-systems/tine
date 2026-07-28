"""Generic helpers shared by tine's Python entry points."""

import errno
import fcntl
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO, cast

# Fall back only when the filesystem does not support cloning or linking.
_CLONE_FALLBACK_ERRNOS = frozenset({errno.ENOTTY, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV})
_LINK_FALLBACK_ERRNOS = frozenset({errno.EMLINK, errno.EOPNOTSUPP, errno.EXDEV})


def terminal_is_dumb() -> bool:
    """Whether terminal styling should be disabled."""
    return not sys.stdout.isatty() or os.getenv("TERM", "") == "dumb"


ANSI_CYAN = "\033[1;36m" if not terminal_is_dumb() else ""
ANSI_GREEN = "\033[32m" if not terminal_is_dumb() else ""
ANSI_RESET = "\033[0m" if not terminal_is_dumb() else ""


def clone_file(src: Path, dst: Path, allow_link: bool = False) -> None:
    """Clone, optionally hardlink, or copy src to dst while preserving its mode.

    Hardlinking requires an explicit opt-in because later writes affect both paths.
    """
    with open(src, "rb") as source, open(dst, "wb") as destination:
        try:
            fcntl.ioctl(destination.fileno(), fcntl.FICLONE, source.fileno())
        except OSError as error:
            if error.errno not in _CLONE_FALLBACK_ERRNOS:
                raise
        else:
            shutil.copymode(src, dst)
            return
    dst.unlink()
    if allow_link:
        try:
            os.link(src, dst)
            return
        except OSError as error:
            if error.errno not in _LINK_FALLBACK_ERRNOS:
                raise
    shutil.copy(src, dst)


def remove_path(path: Path, with_parents: bool = False) -> None:
    """Remove a file, symlink, or whole directory tree, if it is there at all.

    shutil.rmtree only handles real directories: it raises on files, on symlinks even when
    they point at a directory, and on a missing path. With `with_parents`, also take the
    directories the removal leaves empty, up to the first one still in use.
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.is_symlink() or path.exists():
        path.unlink()
    if not with_parents:
        return
    for parent in path.parents:
        # `parent.parent` stops the walk at the filesystem root.
        if parent == parent.parent or not parent.is_dir() or any(parent.iterdir()):
            break
        parent.rmdir()


@contextmanager
def atomic_text_writer(path: Path, *, mode: int | None = None) -> Iterator[TextIO]:
    """Yield a UTF-8 stream and atomically replace its destination on success."""
    path.parent.mkdir(parents=True, exist_ok=True)
    destination_mode = mode
    if destination_mode is None:
        destination_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
            newline="\n",
        ) as stream:
            temporary = Path(stream.name)
            yield cast(TextIO, stream)
        temporary.chmod(destination_mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str, *, mode: int | None = None) -> None:
    """Atomically replace a path with UTF-8 text."""
    with atomic_text_writer(path, mode=mode) as stream:
        stream.write(content)
