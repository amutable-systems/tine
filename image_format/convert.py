#!/usr/bin/python3
"""Convert a composed raw disk image into a distributable output format."""

import subprocess
from pathlib import Path
from typing import TypedDict

import specs


class Spec(TypedDict):
    format: str
    input: str
    out: str


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "convert", argv)

    source = Path(spec["input"])
    out = Path(spec["out"])
    if spec["format"] not in ("qcow2", "raw.zst"):
        raise SystemExit(f"convert: unknown format {spec['format']!r}")
    if spec["format"] == "qcow2":
        # convert drops zero clusters, so the qcow2 stays compact regardless of the raw size.
        cmd = ["qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(source), str(out)]
    else:
        cmd = [
            "zstd", "-q", "-f",
            # use all physical cores; don't use --adapt, it breaks reproducibility
            "--threads=0",
            # useful for mirroring; conflicts with --long, so don't use that
            "--rsyncable",
            # downloads happen more often than builds, so trade slower for better compression
            "-15",  # well above the default level 3, short of the --ultra maximum
            "-o", str(out), str(source),
        ]  # fmt: skip
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
