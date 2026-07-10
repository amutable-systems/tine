"""rpm-extract — the bootstrap ur-tool.

A minimal, dependency-free rpm payload extractor (host Python, stdlib only, via the shared
`rpmfile` framer). It breaks the bootstrap regress: it lays down the tool-rpm subset into chroot1
so a *real* rpm/libdnf5 can take over from there. Payload only — it skips scriptlets, file caps,
ownership, SELinux, device nodes; the real rpm sets those when it builds chroot2.

Supports v4 (070701 "newc" cpio payload) — what Fedora 44 ships. Repository pools normally hand it
their shared, pre-decompressed `.cpio` representation; it also accepts a raw RPM for standalone
use, frames it with `rpmfile`, and decompresses its payload. Both paths unpack with the shared
`cpio` module (also the image packer's writer). v6 (index-keyed payload, per-file metadata from
header tags) is a TODO gated on a pin that uses it; libarchive can't read rpm 6, which is why this
is ours.

Like every stored tree, the extracted chroot goes through `rootfs.capture` at the end: names buck
can't store (systemd's backslash-escaped units, which ride in once the engine carries systemd)
become inert `.esc.` markers. chroot1 is only ever an exec environment (`--tools`), never booted
or mounted as a delta, so the escaped unit files staying escaped is harmless.
"""

import mmap
import sys
import tempfile
from pathlib import Path

import cpio
import rootfs
import rpmfile


def extract(rpm_path: Path, dest: Path) -> int:
    """Extract an rpm's payload into dest. Returns the number of files written.

    Frame the rpm (lead + two headers) with `rpmfile`, decompress the payload, and unpack the newc
    cpio. `cpio.unpack` is fd/mmap-based, so the decompressed payload goes through a temp file; rpm
    payloads are only 4-byte-aligned (not block-aligned), so unpack can't share an extent here
    regardless — it falls back to a userspace copy; only the block-aligned Writer archives clone."""
    with rpm_path.open("rb") as rpm:
        with mmap.mmap(rpm.fileno(), 0, access=mmap.ACCESS_READ) as data:
            _sig, main = rpmfile.headers(data)
        rpm.seek(main.end)
        with tempfile.TemporaryFile() as tmp:
            rpmfile.decompress_stream(rpm, tmp)
            tmp.flush()
            return cpio.unpack(tmp.fileno(), dest)


def extract_payload(payload: Path, dest: Path) -> int:
    with payload.open("rb") as stream:
        return cpio.unpack(stream.fileno(), dest)


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
    for package in rpms:
        total += extract_payload(package, dest) if package.suffix == ".cpio" else extract(package, dest)
    rootfs.capture(dest)
    print(f"extracted {total} files from {len(rpms)} rpm(s) into {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
