"""Shared terminal-image preparation that cannot be persisted in Buck layers."""

import shutil
import subprocess
from pathlib import Path


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
