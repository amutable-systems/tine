#!/usr/bin/python3
"""decompress — stream one raw RPM into its uncompressed payload archive."""

import mmap
import sys
from pathlib import Path

import rpmfile


def decompress(source: Path, output: Path) -> None:
    with source.open("rb") as rpm:
        with mmap.mmap(rpm.fileno(), 0, access=mmap.ACCESS_READ) as data:
            _signature, main = rpmfile.headers(data)
        rpm.seek(main.end)
        with output.open("wb") as payload:
            rpmfile.decompress_stream(rpm, payload)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        raise SystemExit("usage: decompress.py RPM PAYLOAD")
    decompress(Path(args[0]), Path(args[1]))


if __name__ == "__main__":
    main()
