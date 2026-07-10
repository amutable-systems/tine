"""cpio — a shared newc ("070701") cpio reader + writer.

Two consumers: `rpm/extract.py` reads (a decompressed rpm payload → files), and the image
packer + boot driver write (a directory tree → the initrd / kernel-modules cpio). One format
implementation, so the newc quirks live in one place.

Reflink, both directions. The writer block-aligns each large file's data (padding the embedded name
with NULs — newc `namesize` is free-form) so its offset lands on a block boundary, then
`copy_file_range`s it in: the kernel shares extents (copy-on-write on btrfs/XFS) for the aligned part
and copies the misaligned remainder. `unpack()` reads symmetrically, cloning each file's data *out*
by offset. The reader strips the name padding, so it reads our aligned archives and stock ones alike.
An uncompressed newc cpio is a valid kernel initramfs and `ukify --initrd` payload, so nothing here
compresses.
"""

import ctypes
import mmap
import os
import stat
from collections.abc import Buffer, Iterator
from pathlib import Path
from typing import NamedTuple, Self

MAGIC = b"070701"
TRAILER = "TRAILER!!!"
_HEADER = 110  # 6-byte magic + 13 * 8-hex fields
# Reflink alignment target. A fixed constant (not the runtime fs block size) so the archive bytes
# are reproducible regardless of where they're written; 4096 matches btrfs/XFS block size.
_BLOCK = 4096

# copy_file_range(2) reflinks the block-aligned part of a range (shared extents on btrfs/XFS) and
# copies the rest in the kernel. CPython's old-glibc build omits os.copy_file_range, but the symbol
# is in the host libc at runtime, so bind it directly.
_libc = ctypes.CDLL(None, use_errno=True)
_libc.copy_file_range.restype = ctypes.c_ssize_t
# int fd_in, loff_t *off_in, int fd_out, loff_t *off_out, size_t len, unsigned int flags
_libc.copy_file_range.argtypes = (
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int64),
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int64),
    ctypes.c_size_t,
    ctypes.c_uint,
)


def _roundup(n: int, align: int) -> int:
    return n + (-n) % align


def _pwrite_all(fd: int, data: memoryview | bytes, off: int) -> None:
    mv = memoryview(data)
    while mv:
        n = os.pwrite(fd, mv, off)
        mv = mv[n:]
        off += n


def _clone_or_copy(dst_fd: int, dst_off: int, src_fd: int, src_off: int, size: int) -> None:
    """copy_file_range `src_fd[src_off:src_off+size]` → `dst_fd` at `dst_off`: the kernel reflinks the
    block-aligned part (shared extents on btrfs/XFS) and copies the misaligned remainder. Looped for
    short returns; used both directions (a source file into the archive, an archive entry back out)."""
    o_in, o_out, done = ctypes.c_int64(src_off), ctypes.c_int64(dst_off), 0
    while done < size:
        n = _libc.copy_file_range(src_fd, ctypes.byref(o_in), dst_fd, ctypes.byref(o_out), size - done, 0)
        if n < 0:
            raise OSError(ctypes.get_errno(), "copy_file_range")
        if n == 0:
            raise OSError(f"short copy_file_range: {size - done} of {size} bytes remain")
        done += n


# ---- reader ---------------------------------------------------------------------------------


class Entry(NamedTuple):
    """One decoded newc record. `data` is the symlink target or file contents (a view into the
    source buffer); empty for directories and hardlink stubs. `data_off` is that view's byte
    offset within the buffer, so a file-backed reader can clone it out."""

    ino: int
    mode: int
    nlink: int
    devmajor: int
    devminor: int
    name: str
    data: memoryview
    data_off: int


def read(buf: Buffer, start: int = 0) -> Iterator[Entry]:
    """Parse a newc archive from `buf` (bytes or an mmap) beginning at `start`, yielding every
    record up to the TRAILER. Each Entry's offsets are absolute within `buf`, so a reader whose
    `buf` maps a whole file can clone data out of the file by that offset — including when the
    archive is embedded at `start` in a larger file (an rpm payload)."""
    view = memoryview(buf)

    # newc pads headers and file data to 4 bytes *relative to the archive start*. When the archive
    # is embedded at an unaligned `start` (an rpm payload begins right after the main header, which
    # isn't 4-aligned), absolute rounding would drift — so align against `start`.
    def align(pos: int) -> int:
        return start + _roundup(pos - start, 4)

    off = start
    while True:
        if bytes(view[off : off + 6]) != MAGIC:
            raise ValueError(f"bad cpio magic at {off}: {bytes(view[off : off + 6])!r}")
        # 13 8-hex fields: ino, mode, uid, gid, nlink, mtime, filesize, devmajor, devminor,
        # rdevmajor, rdevminor, namesize, check.
        f = [int(bytes(view[off + 6 + i * 8 : off + 6 + (i + 1) * 8]), 16) for i in range(13)]
        ino, mode, nlink, filesize, devmajor, devminor, namesize = (
            f[0], f[1], f[4], f[6], f[7], f[8], f[11],
        )  # fmt: skip
        name_off = off + _HEADER
        raw = bytes(view[name_off : name_off + namesize])
        # First NUL drops both the stock terminator and any of our alignment padding.
        name = raw.split(b"\x00", 1)[0].decode()
        data_off = align(name_off + namesize)
        if name == TRAILER:
            return
        yield Entry(
            ino, mode, nlink, devmajor, devminor, name, view[data_off : data_off + filesize], data_off
        )
        off = align(data_off + filesize)


def _relpath(name: str) -> str:
    """Map an rpm cpio name ('./usr/...') to a dest-relative path, dropping absolute and '..'
    components ('..foo' survives — only exact '..' components are dropped). This sanitizes entry
    *names*, not symlink *targets*: a later entry routed through a hostile symlink could still escape
    dest. We don't defend against that for now — the payloads are trusted Fedora rpms."""
    return "/".join(p for p in name.split("/") if p not in ("", ".", ".."))


def _hardlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.hardlink_to(target)


def _put_regular(target: Path, e: Entry, src_fd: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        _clone_or_copy(fd, 0, src_fd, e.data_off, len(e.data))
    finally:
        os.close(fd)
    target.chmod(e.mode & 0o7777)


def _extract(mm: Buffer, dest: Path, src_fd: int, start: int) -> int:
    """The extraction loop, split from `unpack` so its frame — and the last Entry's memoryview into
    `mm` — is released before `unpack` closes the mmap (a live view would make `mm.close()` raise
    BufferError). Don't inline it back. Hardlink sets (nlink > 1: content on one member, zero-size
    stubs elsewhere) resolve in either arrival order; devices/fifos/sockets are skipped."""
    count = 0
    canonical: dict[tuple[int, int, int], Path] = {}
    pending: dict[tuple[int, int, int], list[tuple[Path, int]]] = {}
    for e in read(mm, start):
        rel = _relpath(e.name)
        if not rel:
            continue
        target = dest / rel
        fmt = e.mode & 0o170000
        # Hardlinkable iff a regular file sharing an inode (nlink > 1).
        key = (e.devmajor, e.devminor, e.ino) if e.nlink > 1 and fmt == 0o100000 else None
        if key is not None and len(e.data) == 0:
            if key in canonical:
                _hardlink(target, canonical[key])
                count += 1
            else:
                pending.setdefault(key, []).append((target, e.mode))
            continue
        if fmt == 0o040000:  # directory
            target.mkdir(parents=True, exist_ok=True)
        elif fmt == 0o120000:  # symlink (last-writer-wins across multi-archive extracts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            target.symlink_to(bytes(e.data).decode())
        elif fmt == 0o100000:  # regular file
            _put_regular(target, e, src_fd)
        else:  # device/fifo/socket
            continue
        count += 1
        if key is not None:  # this entry carried the content; flush deferred links
            canonical[key] = target
            for link, _ in pending.pop(key, []):
                _hardlink(link, target)
                count += 1
    for links in pending.values():  # a fully-empty hardlink set has no content carrier
        for target, mode in links:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"")
            target.chmod(mode & 0o7777)
            count += 1
    return count


def unpack(fd: int, dest: Path, *, offset: int = 0) -> int:
    """Extract the newc archive in `fd` (from byte `offset`) into `dest`, returning entries written —
    the fd-based counterpart to `Writer`, cloning each regular file's data out of `fd` by absolute
    offset. `offset` skips leading framing, so a cpio embedded in a larger file (an rpm payload)
    extracts straight from its fd."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if os.fstat(fd).st_size == 0:
        return 0
    with mmap.mmap(fd, 0, prot=mmap.PROT_READ) as mm:
        return _extract(mm, dest, fd, offset)


# ---- writer ---------------------------------------------------------------------------------


class Writer:
    """Streams a reproducible newc archive. Entries are emitted in caller order (sort first); uid/gid
    are 0, mtimes clamp to `epoch`, and every record has nlink=1 with a monotonic inode counter (no
    hardlink dedup — safe, since the kernel only links when nlink>1). Large files are block-aligned
    and reflinked in (see module docstring). Every write targets an explicit offset (`self._pos`), so
    a reflinking copy_file_range — which doesn't move the fd — slots in between the header writes."""

    def __init__(self, path: Path, epoch: int) -> None:
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        self._epoch = epoch
        self._pos = 0
        self._ino = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if exc[0] is not None:  # error mid-archive: release the fd but don't finalize a partial archive
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            return
        self.close()

    def _write(self, data: bytes) -> None:
        _pwrite_all(self._fd, data, self._pos)
        self._pos += len(data)

    def _pad(self, align: int) -> None:
        if pad := (-self._pos) % align:
            self._write(b"\x00" * pad)

    def _header(self, name: str, mode: int, filesize: int, *, mtime: int, block_align: bool) -> None:
        self._ino += 1
        raw = name.encode()
        namesize = len(raw) + 1  # name + its NUL terminator
        if block_align:
            # Grow the name (namesize is free-form) so header+name reaches a block boundary and the
            # file data that follows is block-aligned for copy_file_range to share extents.
            namesize = _roundup(self._pos + _HEADER + namesize, _BLOCK) - (self._pos + _HEADER)
        fields = (self._ino, mode, 0, 0, 1, min(mtime, self._epoch), filesize, 0, 0, 0, 0, namesize, 0)
        self._write(MAGIC + b"".join(b"%08x" % v for v in fields))
        self._write(raw + b"\x00" * (namesize - len(raw)))
        self._pad(4)

    def add_dir(self, name: str, *, mode: int, mtime: int) -> None:
        self._header(name, stat.S_IFDIR | (mode & 0o7777), 0, mtime=mtime, block_align=False)

    def add_symlink(self, name: str, target: str, *, mtime: int) -> None:
        data = target.encode()
        self._header(name, stat.S_IFLNK | 0o777, len(data), mtime=mtime, block_align=False)
        self._write(data)
        self._pad(4)

    def add_file(self, name: str, src: Path, *, mode: int, mtime: int) -> None:
        fd = os.open(src, os.O_RDONLY)
        try:
            size = os.fstat(fd).st_size
            self._header(name, stat.S_IFREG | (mode & 0o7777), size, mtime=mtime, block_align=size >= _BLOCK)
            _clone_or_copy(self._fd, self._pos, fd, 0, size)
            self._pos += size
        finally:
            os.close(fd)
        self._pad(4)

    def close(self) -> None:
        if self._fd < 0:
            return
        # TRAILER!!!: nlink=1, ino 0, everything else zero (the stock convention).
        fields = (0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, len(TRAILER) + 1, 0)
        self._write(MAGIC + b"".join(b"%08x" % v for v in fields))
        self._write(TRAILER.encode() + b"\x00")
        self._pad(4)
        os.close(self._fd)
        self._fd = -1


def pack_tree(tree: Path, out: Path, epoch: int, *, subtree: str | None = None) -> int:
    """Write `tree` (or its `subtree`) into the newc archive `out`, returning entries written.

    Directory entries precede their contents (sorted rglob is parent-first). With `subtree`, its
    ancestor dirs are emitted first so the extracted layout is rooted correctly (e.g. the
    kernel-modules cpio keeps its `usr/lib/modules/<kver>/...` prefix)."""
    tree = Path(tree)
    if subtree is not None:
        parts = Path(subtree).parts
        paths = [tree.joinpath(*parts[:i]) for i in range(1, len(parts) + 1)]
        paths += sorted((tree / subtree).rglob("*"))
    else:
        paths = sorted(tree.rglob("*"))
    count = 0
    with Writer(out, epoch) as w:
        for path in paths:
            st = path.lstat()
            name = str(path.relative_to(tree))
            mode = stat.S_IMODE(st.st_mode)
            mtime = int(st.st_mtime)
            if stat.S_ISDIR(st.st_mode):
                w.add_dir(name, mode=mode, mtime=mtime)
            elif stat.S_ISLNK(st.st_mode):
                w.add_symlink(name, os.readlink(path), mtime=mtime)
            elif stat.S_ISREG(st.st_mode):
                w.add_file(name, path, mode=mode, mtime=mtime)
            else:
                continue  # devices/fifos/sockets: unneeded in an initramfs (systemd mounts /dev)
            count += 1
    return count
