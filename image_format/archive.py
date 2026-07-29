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
import tempfile
from collections.abc import Callable
from pathlib import Path

import util

import cpio
import finalize


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


def _pack(tree: Path, out: Path, fmt: str, epoch: int) -> None:
    if fmt == "tar":
        _tar(tree, out, epoch)
    elif fmt == "cpio":
        cpio.pack_tree(tree, out, epoch)
    else:
        raise SystemExit(f"unknown archive format {fmt!r}")


def _compress(src: Path, out: Path) -> None:
    """Compress a finished archive with zstd."""
    subprocess.run(
        [
            "zstd", "-q", "-f",
            # zstd's multi-threaded output is byte-identical to its single-threaded output, so using
            # every core stays reproducible. --adapt would not, so it stays out.
            "--threads=0",
            # Level 9 is the sweet spot: it beats the default 3 by 10% in a fraction of a second,
            # where 19 buys another 10% but takes 28 times as long.
            "-9",
            "-o", str(out), str(src),
        ],
        check=True,
    )  # fmt: skip


def _archive(tree: Path, out: Path, fmt: str, epoch: int, compression: str) -> None:
    if fmt == "directory":
        if compression != "none":
            raise SystemExit("archive: the directory format cannot be compressed")

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
    elif compression == "none":
        _pack(tree, out, fmt, epoch)
    else:
        # zstd needs the finished archive, so pack it beside the output rather than in TMPDIR: the
        # shared filesystem keeps the packer's reflink cloning working, and Buck only ever sees the
        # compressed result.
        with tempfile.TemporaryDirectory(dir=out.parent) as scratch:
            raw = Path(scratch) / out.name
            _pack(tree, raw, fmt, epoch)
            _compress(raw, out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="archive")
    finalize.add_arguments(parser)
    parser.add_argument("--out", required=True, help="output archive or directory")
    parser.add_argument("--format", required=True, choices=("tar", "cpio", "directory"))
    parser.add_argument("--compression", default="none", choices=("none", "zstd"))
    parser.add_argument(
        "--pkgdb-path",
        action="append",
        default=[],
        metavar="PATH",
        help="image path holding the package database, to strip from the archive (repeatable)",
    )
    args = parser.parse_args(argv)

    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    out = Path(args.out).resolve()
    with finalize.image(args, program="archive") as tree:
        # The package database is a supply-chain artifact that the image's `[pkgdb]` subtarget
        # captures separately, so an archive nothing resolves packages in can drop it.
        for relative in args.pkgdb_path:
            util.remove_path(tree / relative, with_parents=True)
        _archive(tree, out, args.format, epoch, args.compression)
    print(f"archive: wrote {args.format} (epoch={epoch}) -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
