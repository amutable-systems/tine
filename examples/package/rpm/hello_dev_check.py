#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Check one dev-mode build of the hello fixture."""

import subprocess
import sys
from pathlib import Path

rpms = Path(sys.argv[1])


def query(package: Path, *args: str) -> str:
    return subprocess.run(
        ["rpm", "-qp", *args, package], check=True, stdout=subprocess.PIPE, text=True
    ).stdout


packages = list(rpms.glob("hello-[0-9]*.rpm"))
assert len(packages) == 1, packages
[package] = packages
assert query(package, "--queryformat", "%{PAYLOADCOMPRESSOR}") == "(none)"
files = set(query(package, "-l").splitlines())
markers = "/usr/share/tine-package"
assert f"{markers}/spec-from-checkout" in files, files
assert f"{markers}/profile-lto-disabled" in files, files
assert f"{markers}/profile-annobin-disabled" in files, files
assert not list(rpms.glob("hello-debuginfo-*.rpm")), sorted(rpms.iterdir())
print(f"hello: dev build, {len(files)} files")
