"""Shared terminal-image preparation that cannot be persisted in Buck layers."""

import argparse
import contextlib
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import rootfs


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare the driver half of the terminal_image_command contract."""
    parser.add_argument("--lower", action="append", default=[], help="image delta (bottom..top)")
    parser.add_argument(
        "--tmpfiles", action="append", default=[], help="authored tmpfiles.d snippet (repeatable)"
    )


@contextlib.contextmanager
def image(
    args: argparse.Namespace,
    *,
    program: str,
    lowers: list[str | Path] | None = None,
    binds: list[tuple[str | Path, str | Path]] | None = None,
) -> Iterator[Path]:
    """Mount the image stack from `add_arguments` options and yield the finalized tree."""
    with rootfs.rootfs("/buildroot", lowers=args.lower if lowers is None else lowers, binds=binds) as tree:
        apply_tmpfiles(tree, args.tmpfiles, program=program)
        yield tree


def apply_tmpfiles(tree: Path, snippets: list[str], *, program: str) -> None:
    """Apply authored tmpfiles configuration beneath `tree`."""
    if not snippets:
        return
    tmpfiles = shutil.which("systemd-tmpfiles")
    if tmpfiles is None:
        raise SystemExit(f"{program}: systemd-tmpfiles is required to finalize this image")

    config = "".join(snippet if snippet.endswith("\n") else snippet + "\n" for snippet in snippets)
    subprocess.run(
        [tmpfiles, "--create", f"--root={tree}", "-"],
        input=config,
        text=True,
        check=True,
    )
