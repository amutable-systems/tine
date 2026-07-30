"""Decompress a zstd-compressed tool binary downloaded by `http_tool`."""

import compression.zstd
import shutil
import sys
from pathlib import Path


def main() -> None:
    source, destination = (Path(argument) for argument in sys.argv[1:])
    with compression.zstd.ZstdFile(source) as reader, destination.open("wb") as writer:
        shutil.copyfileobj(reader, writer)
    destination.chmod(0o755)


if __name__ == "__main__":
    main()
