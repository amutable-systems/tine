#!/usr/bin/python3
"""Merge a logical image into a deterministic tar, cpio, or directory.

Archive ownership is normalized to uid/gid 0. Tar stores extended attributes as PAX
headers; the newc cpio format has no general extended-attribute representation.
"""

import argparse
import os
import subprocess
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path

import finalize

import cpio
import rootfs


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


def _clamp_mtimes(tree: Path, epoch: int) -> None:
    """Clamp directory-format mtimes without an archive filter."""
    for path in [tree, *tree.rglob("*")]:
        if path.lstat().st_mtime > epoch:
            os.utime(path, (epoch, epoch), follow_symlinks=False)


def _tar(tree: Path, out: Path, epoch: int) -> None:
    # Explicit sorted entries keep archive order stable.
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(tree.rglob("*")):
            archive.add(
                path,
                arcname="./" + str(path.relative_to(tree)),
                recursive=False,
                filter=_reproducible(path, epoch),
            )


def _archive(tree: Path, out: Path, fmt: str, epoch: int) -> None:
    if fmt == "directory":
        # Reject names that would wedge Buck while storing the thawed tree.
        for path in tree.rglob("*"):
            if "\\" in path.name:
                raise SystemExit(
                    f"archive: {path.relative_to(tree)} contains a backslash, which buck cannot "
                    "store; the directory format cannot represent this image — use tar"
                )
        out.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-a", "--reflink=auto", f"{tree}/.", str(out)], check=True)
        _clamp_mtimes(out, epoch)
    elif fmt == "tar":
        _tar(tree, out, epoch)
    elif fmt == "cpio":
        cpio.pack_tree(tree, out, epoch)
    else:
        raise SystemExit(f"unknown archive format {fmt!r}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="archive")
    parser.add_argument("--lower", action="append", default=[], help="image delta (bottom..top)")
    parser.add_argument("--out", required=True, help="output archive or directory")
    parser.add_argument("--format", required=True, choices=("tar", "cpio", "directory"))
    parser.add_argument(
        "--tmpfiles", action="append", default=[], help="authored tmpfiles.d line (repeatable)"
    )
    args = parser.parse_args(argv)

    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    out = Path(args.out).resolve()
    with rootfs.rootfs("/buildroot", lowers=args.lower) as tree:
        finalize.apply_tmpfiles(
            tree,
            args.tmpfiles,
            program="archive",
        )
        _archive(tree, out, args.format, epoch)
    print(f"archive: wrote {args.format} (epoch={epoch}) -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
