#!/usr/bin/python3
"""Re-encode a Cargo.lock as JSON.

A dynamic action reads an artifact as text or as JSON and nothing else, and a lock is TOML, so the
fetches it describes can only be declared from a converted copy.
"""

import json
import sys
import tomllib
from pathlib import Path


def main() -> None:
    lock, out = (Path(argument) for argument in sys.argv[1:])
    resolved = tomllib.loads(lock.read_text(encoding="utf-8"))
    out.write_text(json.dumps(resolved, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
