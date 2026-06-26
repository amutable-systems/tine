"""rpm-extract — the bootstrap ur-tool.

A minimal, dependency-free rpm payload extractor (host Python >= 3.14 only:
stdlib compression.zstd/lzma/zlib). It breaks the bootstrap regress: it lays
down the tool-rpm subset into chroot1 so a *real* rpm/libdnf5 can take over from
there. Payload only — it skips scriptlets, file caps, ownership, SELinux, device
nodes; the real rpm sets those when it builds chroot2.

Supports v4 (070701 "newc" cpio payload) — what Fedora 44 ships (zstd+cpio).
v6 (index-keyed payload, per-file metadata from header tags) is a TODO gated on
a pin that uses it; libarchive can't read rpm 6, which is why this is ours.
"""

import compression.zstd
import lzma
import struct
import sys
import zlib
from pathlib import Path

HEADER_MAGIC = b"\x8e\xad\xe8\x01"
LEAD_SIZE = 96
CPIO_TRAILER = "TRAILER!!!"


def _skip_header(data: bytes, off: int, *, pad_to_8: bool) -> int:
    """Return the offset past one rpm header section (intro + index + data store).

    We only need framing to reach the payload — not the tag values. The signature
    header is padded to an 8-byte boundary; the main header is not (payload
    follows immediately).
    """
    if data[off : off + 4] != HEADER_MAGIC:
        raise SystemExit(f"bad rpm header magic: {data[off : off + 4].hex()}")
    nindex, nbytes = struct.unpack(">II", data[off + 8 : off + 16])
    off += 16 + 16 * nindex + nbytes
    if pad_to_8 and off % 8:
        off += 8 - (off % 8)
    return off


def _decompress(payload: bytes) -> bytes:
    """Decompress an rpm payload, sniffing the compressor by magic bytes."""
    if payload[:4] == b"\x28\xb5\x2f\xfd":
        return compression.zstd.decompress(payload)
    if payload[:6] == b"\xfd7zXZ\x00":
        return lzma.decompress(payload)
    if payload[:2] == b"\x1f\x8b":
        return zlib.decompress(payload, wbits=zlib.MAX_WBITS | 16)
    raise SystemExit(f"unknown payload compressor (magic {payload[:6].hex()})")


def _relpath(name: str) -> str:
    """Map an rpm cpio name ('./usr/...') to a safe dest-relative path.

    Drop the leading './' plus any absolute or '..' components so a malformed or
    hostile payload can't escape dest. A literal name like '..foo' survives —
    only exact '..' path components are dropped.
    """
    return "/".join(p for p in name.split("/") if p not in ("", ".", ".."))


def _hardlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.hardlink_to(target)


def _extract_cpio(data: bytes, dest: Path) -> int:
    """Extract a 070701 'newc' cpio archive into dest. Returns entries written."""
    off = 0
    count = 0
    # A hardlink set (nlink > 1) stores its data only on the *last* entry; the
    # earlier links carry filesize 0. Map each inode to the path written with its
    # content, and stash links seen before that content so we can link them once
    # it arrives (handles either order; the common case is data-last).
    canonical: dict[tuple[int, int, int], Path] = {}
    pending: dict[tuple[int, int, int], list[tuple[Path, int]]] = {}
    while True:
        if data[off : off + 6] != b"070701":
            raise SystemExit(f"bad cpio entry magic at {off}: {data[off : off + 6]!r}")
        # newc header: magic + 13 8-hex fields: ino, mode, uid, gid, nlink, mtime,
        # filesize, devmajor, devminor, rdevmajor, rdevminor, namesize, check.
        f = [int(data[off + 6 + i * 8 : off + 6 + (i + 1) * 8], 16) for i in range(13)]
        ino, mode, nlink, filesize, devmajor, devminor, namesize = (
            f[0], f[1], f[4], f[6], f[7], f[8], f[11],
        )  # fmt: skip
        name_off = off + 110
        name = data[name_off : name_off + namesize - 1].decode()  # drop NUL
        data_off = name_off + namesize
        data_off += (-data_off) % 4  # name + header padded to a multiple of 4
        off = data_off + filesize
        off += (-off) % 4
        if name == CPIO_TRAILER:
            break
        rel = _relpath(name)
        if not rel:
            continue
        target = dest / rel
        # Hardlinkable iff a regular file sharing an inode (nlink > 1).
        key = (devmajor, devminor, ino) if nlink > 1 and mode & 0o170000 == 0o100000 else None
        if key is not None and filesize == 0:
            if key in canonical:
                _hardlink(target, canonical[key])
                count += 1
            else:
                pending.setdefault(key, []).append((target, mode))
            continue
        if not _write_entry(target, mode, data[data_off : data_off + filesize]):
            continue
        count += 1
        if key is not None:  # this entry carried the content; flush deferred links
            canonical[key] = target
            for link, _ in pending.pop(key, []):
                _hardlink(link, target)
                count += 1
    # A fully-empty hardlink set has no content-carrying entry; lay each down.
    for links in pending.values():
        for target, mode in links:
            if _write_entry(target, mode, b""):
                count += 1
    return count


def _write_entry(target: Path, mode: int, content: bytes) -> bool:
    """Lay down one cpio entry at target. Returns True if anything was written."""
    fmt = mode & 0o170000
    if fmt == 0o040000:  # directory
        target.mkdir(parents=True, exist_ok=True)
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    if fmt == 0o120000:  # symlink (last-writer-wins across multi-rpm extracts)
        target.unlink(missing_ok=True)
        target.symlink_to(content.decode())
        return True
    if fmt == 0o100000:  # regular file (payload-only: skip devices/fifos)
        target.write_bytes(content)
        target.chmod(mode & 0o7777)
        return True
    return False


def extract(rpm_path: Path, dest: Path) -> int:
    """Extract an rpm's payload into dest. Returns the number of files written."""
    dest.mkdir(parents=True, exist_ok=True)
    data = rpm_path.read_bytes()
    off = _skip_header(data, LEAD_SIZE, pad_to_8=True)  # signature header
    off = _skip_header(data, off, pad_to_8=False)  # main header
    return _extract_cpio(_decompress(data[off:]), dest)


def _expand(paths: list[Path]) -> list[Path]:
    """Expand any directory argument to the (sorted) files it contains."""
    out: list[Path] = []
    for p in paths:
        out += sorted(p.iterdir()) if p.is_dir() else [p]
    return out


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        raise SystemExit("usage: rpm-extract.py DEST RPM|DIR [RPM|DIR...]")
    dest, rpms = Path(args[0]), _expand([Path(p) for p in args[1:]])
    total = 0
    for rpm_path in rpms:
        total += extract(rpm_path, dest)
    print(f"extracted {total} files from {len(rpms)} rpm(s) into {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
