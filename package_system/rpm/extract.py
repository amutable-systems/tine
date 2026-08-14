"""Bootstrap a box by extracting RPM v4 newc payloads without RPM tooling.

An rpm is read as its repository serves it: the header is framed off, the payload decompressed,
and the cpio unpacked. Metadata and scriptlets are deferred to the real install.
"""

import mmap
import tempfile
from pathlib import Path

import cpio
import extractor
import rpmfile


def extract(rpm_path: Path, dest: Path) -> int:
    """Extract an RPM payload into dest and return the number of files written."""
    with rpm_path.open("rb") as rpm:
        with mmap.mmap(rpm.fileno(), 0, access=mmap.ACCESS_READ) as data:
            payload = rpmfile.payload_offset(data)
        rpm.seek(payload)
        with tempfile.TemporaryFile() as tmp:
            rpmfile.decompress_stream(rpm, tmp)
            tmp.flush()
            return cpio.unpack(tmp.fileno(), dest)


def extract_payload(payload: Path, dest: Path) -> int:
    with payload.open("rb") as stream:
        return cpio.unpack(stream.fileno(), dest)


def unpack(package: Path, dest: Path) -> int:
    """Extract an rpm, or a payload already framed off one."""
    return extract_payload(package, dest) if package.suffix == ".cpio" else extract(package, dest)


def main(argv: list[str] | None = None) -> None:
    extractor.run("extract", unpack, argv)


if __name__ == "__main__":
    main()
