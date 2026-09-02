#!/usr/bin/python3
"""Check one regular archive build: a binary and a source package, both with compressed payloads."""

import subprocess
import sys
from pathlib import Path

rpms = sorted(Path(sys.argv[1]).glob("hello-*.rpm"))
sources = [rpm for rpm in rpms if rpm.name.endswith(".src.rpm")]
assert len(rpms) == 2 and len(sources) == 1, rpms
for rpm in rpms:
    query = ["rpm", "-qp", "--queryformat", "%{PAYLOADCOMPRESSOR}", rpm]
    compressor = subprocess.run(query, check=True, stdout=subprocess.PIPE, text=True).stdout
    assert compressor in {"gzip", "bzip2", "xz", "lzma", "zstd"}, (rpm, compressor)
print(f"archive: {len(rpms)} packages with compressed payloads")
