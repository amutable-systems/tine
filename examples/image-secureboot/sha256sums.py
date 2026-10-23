#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Write the SHA256SUMS.<arch> of a release's update payloads.

This is the author hook of the example release. It picks the payloads by their kind in the release's
description, so it never parses a file name. A step that publishes the releases can combine the files of
several architectures into the SHA256SUMS that systemd-sysupdate reads.
"""

import argparse
import hashlib
import json
from pathlib import Path

# These are the kinds of file systemd-sysupdate transfers.
PAYLOAD_KINDS = frozenset({"extension", "partition", "portable", "uki"})


def main() -> None:
    parser = argparse.ArgumentParser(prog="sha256sums", description=__doc__.split("\n\n")[0])
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--files", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    description = json.loads(args.artifacts.read_text(encoding="utf-8"))
    files = description["files"]
    lines = []
    for name in sorted(files):
        if files[name]["kind"] in PAYLOAD_KINDS:
            with (args.files / name).open("rb") as payload:
                lines.append(f"{hashlib.file_digest(payload, 'sha256').hexdigest()}  {name}\n")
    out = args.out_dir / f"SHA256SUMS.{description['arch']}"
    out.write_text("".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
