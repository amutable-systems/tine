#!/usr/bin/python3
"""Merge a boot-ready logical image into an offline systemd-repart GPT image."""

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import finalize

import rootfs

_SEED_NAMESPACE = uuid.UUID("5af2de99-4f9f-4e0b-a04b-bde36b068c4f")


def _derived_seed(identity: str, definitions: Path) -> uuid.UUID:
    """Derive a stable seed from target identity and partition configuration."""
    digest = hashlib.sha256(identity.encode())
    for definition in sorted(definitions.iterdir()):
        digest.update(b"\0" + definition.name.encode() + b"\0")
        digest.update(definition.read_bytes())
    return uuid.uuid5(_SEED_NAMESPACE, digest.hexdigest())


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="disk")
    p.add_argument("--lower", action="append", default=[], help="the layer stack (bottom..top) to merge")
    p.add_argument("--out", required=True, help="the output raw disk image")
    p.add_argument("--identity", required=True, help="stable target identity used to derive the seed")
    p.add_argument("--seed", help="explicit GPT/partition UUID seed")
    p.add_argument(
        "--tmpfiles", action="append", default=[], help="authored tmpfiles.d snippet (repeatable)"
    )
    p.add_argument(
        "--definition",
        action="append",
        required=True,
        metavar="PATH",
        help="rendered repart.d definition in partition order",
    )
    args = p.parse_args(argv)
    out = Path(args.out).resolve()

    with (
        rootfs.rootfs("/buildroot", lowers=args.lower) as tree,
        tempfile.TemporaryDirectory(prefix="disk.") as scratch,
    ):
        definitions = Path(scratch) / "repart.d"
        definitions.mkdir()
        for index, src in enumerate(args.definition):
            shutil.copyfile(src, definitions / f"{index:04}.conf")

        finalize.apply_tmpfiles(
            tree,
            args.tmpfiles,
            include_image_config=False,
            program="disk",
        )
        seed = uuid.UUID(args.seed) if args.seed else _derived_seed(args.identity, definitions)

        subprocess.run(
            [
                "systemd-repart",
                "--empty=create",
                "--size=auto",
                "--dry-run=no",
                "--json=pretty",
                "--no-pager",
                f"--root={tree}",
                "--offline=yes",
                "--seed", str(seed),
                "--definitions", str(definitions),
                str(out),
            ],
            check=True,
        )  # fmt: skip
    print(f"disk: wrote {out.name} (seed={seed})", file=sys.stderr)


if __name__ == "__main__":
    main()
