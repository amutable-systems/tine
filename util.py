"""Generic helpers shared by tine's Python entry points."""

import bz2
import compression.zstd
import errno
import fcntl
import gzip
import http.client
import io
import lzma
import os
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, TextIO, cast

# Fall back only when the filesystem does not support cloning or linking.
_CLONE_FALLBACK_ERRNOS = frozenset({errno.ENOTTY, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV})
_LINK_FALLBACK_ERRNOS = frozenset({errno.EMLINK, errno.EOPNOTSUPP, errno.EXDEV})

_TRANSIENT_HTTP_STATUS = frozenset((408, 429, 500, 502, 503, 504))
_FETCH_ATTEMPTS = 4


# What a compressed stream starts with, and what opens it. A file's name is not authoritative
# about how it was compressed, and a repository is free to change compressor between releases,
# so every reader here selects one from the bytes instead.
_COMPRESSORS = (
    (b"\x28\xb5\x2f\xfd", lambda stream: compression.zstd.ZstdFile(stream, mode="rb")),
    (b"\x1f\x8b", lambda stream: gzip.GzipFile(fileobj=stream, mode="rb")),
    (b"\xfd7zXZ\x00", lambda stream: lzma.LZMAFile(stream, mode="rb")),
    (b"BZh", lambda stream: bz2.BZ2File(stream, mode="rb")),
)

# Enough leading bytes to tell every compressor above apart.
MAGIC = 6


def decompressor(magic: bytes) -> Callable[[IO[bytes]], io.BufferedIOBase] | None:
    """What opens a stream beginning with `magic`, or None where it names no compression.

    What an uncompressed stream is then taken to be is the caller's: each reader here expects a
    different thing underneath, and treating the wrong one as valid is how a corrupt download
    becomes a confusing parse error instead of an honest one.
    """
    for prefix, opener in _COMPRESSORS:
        if magic.startswith(prefix):
            return opener
    return None


def urlopen(url: str, *, agent: str) -> http.client.HTTPResponse:
    """Open one URL, naming the tool that asks.

    CDN bot filters (e.g. Cloudflare's) reject Python's default Python-urllib agent.
    """
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": agent}))


def with_retries[T](what: str, operation: Callable[[], T]) -> T:
    """Run one network operation, retrying transient connection failures and HTTP errors."""
    for attempt in range(1, _FETCH_ATTEMPTS + 1):
        try:
            return operation()
        except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
            permanent = (
                isinstance(error, urllib.error.HTTPError) and error.code not in _TRANSIENT_HTTP_STATUS
            )
            if permanent or attempt == _FETCH_ATTEMPTS:
                raise
            print(f"{what}: {error}; retrying…", file=sys.stderr)
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


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


def take_binaries(built: Path, binaries: dict[str, str], *, tool: str, where: str) -> None:
    """Copy each declared binary out of a build tree.

    A name the build did not produce is a fatal error. `tool` prefixes that failure and `where`
    names the tree in it, both in the terms of the ecosystem the calling driver builds for.
    """
    missing = [name for name in binaries if not (built / name).is_file()]
    if missing:
        # Everything executable in there, which after an earlier build of the same project may name
        # more than this one produced.
        found = sorted(
            entry.name for entry in built.iterdir() if entry.is_file() and os.access(entry, os.X_OK)
        )
        raise SystemExit(f"{tool}: no {', '.join(missing)} in {where}, which holds: {', '.join(found)}")
    for name, out in binaries.items():
        # Replace, never rewrite: buck does not clear a kept action's outputs, and whatever consumed
        # the previous binary may hold a hard link to it.
        Path(out).unlink(missing_ok=True)
        clone_file(built / name, Path(out))


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
