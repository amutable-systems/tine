"""Bootstrap an engine by extracting RPM v4 newc payloads without RPM tooling.

Repository pools normally provide pre-decompressed cpio, while raw RPM support
keeps the tool usable alone. Metadata and scriptlets are deferred to the real install.
"""

import mmap
import sys
import tempfile
from pathlib import Path

import cpio
import rootfs
import rpmfile


def extract(rpm_path: Path, dest: Path) -> int:
    """Extract an RPM payload into dest and return the number of files written."""
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
