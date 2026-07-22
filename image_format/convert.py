#!/usr/bin/python3
"""Convert a composed raw disk image into a distributable output format."""

import argparse
import subprocess
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="convert")
    p.add_argument("--format", required=True, choices=["qcow2", "raw.zst"])
    p.add_argument("--input", required=True, help="source raw disk image")
    p.add_argument("--out", required=True, help="converted output path")
    args = p.parse_args(argv)

    source = Path(args.input).resolve()
    out = Path(args.out).resolve()
    if args.format == "qcow2":
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
