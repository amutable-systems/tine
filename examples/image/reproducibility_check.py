#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# Check ./expected.json against actual build targets for one architecture.
# On a mismatch, write the actual digests to OUTPUT, so that CI or a developer can update
# expected.json with it for intended changes.
#
# Arguments: ARCHITECTURE OUTPUT, then NAME DIGEST PATH for each tracked target.

import hashlib
import json
import sys
from pathlib import Path


def main() -> None:
    architecture, output, *entries = sys.argv[1:]
    actual = {}
    changed = []
    for name, expected, path in zip(entries[0::3], entries[1::3], entries[2::3], strict=True):
        with Path(path).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        print(f"{digest}  {path}")
        actual[name] = {"sha256": digest}
        if digest != expected:
            changed.append(name)

    if not changed:
        # left behind by an earlier failure, and no longer true
        Path(output).unlink(missing_ok=True)
        return

    Path(output).write_text(
        json.dumps({architecture: actual}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    sys.exit(f"changed digests: {', '.join(changed)}; {output} has the current ones")


if __name__ == "__main__":
    main()
