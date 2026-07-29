"""Shared terminal-image preparation that cannot be persisted in Buck layers."""

import contextlib
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TypedDict

import rootfs


class ImageSpec(TypedDict):
    """The half of a terminal driver's spec that names the image itself."""

    lower: list[str]
    tmpfiles: list[str]


@contextlib.contextmanager
def image(
    spec: ImageSpec,
    *,
    program: str,
    lowers: Sequence[str | Path] | None = None,
    binds: Sequence[tuple[str | Path, str | Path]] | None = None,
) -> Iterator[Path]:
    """Mount the image stack the spec names and yield the finalized tree."""
    with rootfs.rootfs(
        "/buildroot", lowers=spec["lower"] if lowers is None else lowers, binds=binds
    ) as tree:
        apply_tmpfiles(tree, spec["tmpfiles"], program=program)
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
