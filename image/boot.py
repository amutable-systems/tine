#!/usr/bin/python3
"""Install boot artifacts and bootloader state into one persisted image delta."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import rootfs


def _destination(tree: Path, value: str) -> Path:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"boot: destination must be an absolute image path: {value!r}")
    return tree.joinpath(*path.parts[1:])


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy2)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="boot")
    parser.add_argument("--lower", action="append", default=[], help="image delta (bottom..top)")
    parser.add_argument("--out", required=True, help="this boot layer's overlay upper")
    parser.add_argument("--work", required=True, help="throwaway overlay workdir")
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        nargs=2,
        metavar=("SOURCE", "DESTINATION"),
        help="boot artifact and absolute image destination",
    )
    parser.add_argument("--systemd-boot", action="store_true", help="install systemd-boot")
    args = parser.parse_args(argv)

    files = [(Path(source).resolve(), destination) for source, destination in args.file]
    with rootfs.rootfs(
        "/buildroot",
        lowers=args.lower,
        upperdir=Path(args.out).resolve(),
        workdir=Path(args.work).resolve(),
    ) as tree:
        for source, destination in files:
            _copy(source, _destination(tree, destination))

        if args.systemd_boot:
            (tree / "efi").mkdir(exist_ok=True)
            subprocess.run(
                [
                    "bootctl",
                    "install",
                    f"--root={tree}",
                    "--install-source=image",
                    "--all-architectures",
                    "--no-variables",
                ],
                env=os.environ | {"SYSTEMD_ESP_PATH": "/efi", "SYSTEMD_XBOOTLDR_PATH": "/boot"},
                check=True,
            )
            (tree / "efi/loader/random-seed").unlink(missing_ok=True)
    print(f"boot: installed {len(files)} artifact(s) -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
