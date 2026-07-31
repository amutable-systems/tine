#!/usr/bin/python3
"""Build a Rust project from source inside an engine, against its vendored crate tree.

The build runs with no network: every registry crate is already unpacked, and pointing cargo's
crates-io source at that directory is what keeps it from consulting the registry index. Each git
source is replaced by the fetched repository itself, a file:// clone cargo takes its own checkout
from, insisting on the locked commit, so nothing has to be rewritten or re-verified here.
cargo-auditable wraps the build to record the crate graph in each binary, which is how the image's
SBOM learns what went into it.
"""

import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import TypedDict

import specs
import util


class GitSource(TypedDict):
    # The fields cargo identifies the source by (`git` plus the reference the project asked for).
    fields: dict[str, str]
    # The fetched repository providing the locked commit.
    repo: str


class Spec(TypedDict):
    # The cargo-auditable wrapper cargo builds through.
    auditable: str
    # Declared binary name -> the output to write it to.
    binaries: dict[str, str]
    # Locked commit -> the git source to replace with its fetched repository.
    git: dict[str, GitSource]
    # Where the workspace sits inside `src`, empty when the project is its own root.
    root: str
    # The project's source tree.
    src: str
    # The unpacked crates the build resolves against.
    vendor: str


# Any manifest table that can carry a dependency, including cargo's deprecated underscore spellings,
# plus `target` and `workspace`, whose subtables can hide one. Deliberately too broad: a harmless match
# only asks for a Cargo.lock the project could have committed anyway, while a miss would let it build
# unlocked and fail later in cargo's own resolver.
_DEPENDENCY_TABLES = (
    "build-dependencies",
    "build_dependencies",
    "dependencies",
    "dev-dependencies",
    "dev_dependencies",
    "target",
    "workspace",
)


def _reject_unlocked_dependencies(workspace: Path) -> None:
    """A manifest that names any dependency table must come with the lock cargo writes.

    Without one there is nothing to build the vendored tree from, and the failure cargo itself
    produces — offline resolution against an empty registry — points at the network rather than at
    the missing file.
    """
    manifest = tomllib.loads((workspace / "Cargo.toml").read_text(encoding="utf-8"))
    declared = [table for table in _DEPENDENCY_TABLES if manifest.get(table)]
    if declared:
        raise SystemExit(f"cargo-build: [{declared[0]}] without a Cargo.lock; commit the lock cargo writes")


def _reject_local_config(build: Path, workspace: Path) -> None:
    """Refuse a checkout's own cargo configuration, which would outrank the one this driver writes.

    Cargo merges configuration from the working directory upward and reads `$CARGO_HOME` last, so a
    file in the project wins: it can redirect the crates-io source away from the vendored tree, or
    move the directory the declared binaries are taken from.
    """
    directory = workspace
    while True:
        for name in ("config.toml", "config"):
            found = directory / ".cargo" / name
            if found.exists():
                raise SystemExit(
                    f"cargo-build: {found.relative_to(build)} would override the vendored source "
                    "configuration; keep it out of srcs"
                )
        if directory == build:
            return
        directory = directory.parent


def _take_binaries(built: Path, binaries: dict[str, str]) -> None:
    """Copy each declared binary out; a name the build did not produce is a mis-declaration."""
    missing = [name for name in binaries if not (built / name).is_file()]
    if missing:
        produced = sorted(
            entry.name for entry in built.iterdir() if entry.is_file() and os.access(entry, os.X_OK)
        )
        raise SystemExit(
            f"cargo-build: no {', '.join(missing)} in target/release; "
            f"the build produced: {', '.join(produced)}"
        )
    for name, out in binaries.items():
        util.clone_file(built / name, Path(out))


def _cargo_config(vendor: Path, git: dict[str, GitSource]) -> str:
    """Point the registry at the vendored directory and every git source at its fetched repository.

    Cargo replaces a source as a whole, so each git dependency needs a stanza of its own: one
    section carrying the fields cargo identifies the source by (the section names are arbitrary),
    replaced with one naming the local repository that stands in for the remote.
    """
    sections = ['[source.crates-io]\nreplace-with = "vendored-sources"\n']
    for commit, source in sorted(git.items()):
        rendered = "".join(f'{name} = "{value}"\n' for name, value in source["fields"].items())
        local = f"git-{commit[:12]}"
        sections.append(f'[source."{local}-upstream"]\n{rendered}replace-with = "{local}"\n')
        repo = Path(source["repo"]).resolve()
        sections.append(f'[source.{local}]\ngit = "file://{repo}"\nrev = "{commit}"\n')
    sections.append(f'[source.vendored-sources]\ndirectory = "{vendor}"\n')
    return "\n".join(sections)


def main(argv: list[str] | None = None) -> None:
    spec: Spec = specs.parse("cargo-build", argv)

    # Cargo needs somewhere to write, and the build inputs are read-only artifacts. The sandbox
    # backs /var/tmp with the action's scratch space, which Buck clears before each execution, so
    # fixed names neither collide with a preserved failed tree nor accumulate across builds.
    build = Path("/var/tmp/build")
    cargo_home = Path("/var/tmp/cargo")

    shutil.copytree(spec["src"], build)

    workspace = build / spec["root"]
    _reject_local_config(build, workspace)

    cargo_home.mkdir(parents=True)
    (cargo_home / "config.toml").write_text(
        _cargo_config(Path(spec["vendor"]).resolve(), spec["git"]),
        encoding="utf-8",
    )

    # A project that resolves nothing carries no lock for --locked to hold cargo to. The empty
    # vendored source and the unshared network are what keep such a build from resolving anything.
    locked = ["--locked"]
    if not (workspace / "Cargo.lock").exists():
        _reject_unlocked_dependencies(workspace)
        locked = []
    # Cargo refuses every git transfer under --offline, the file:// repositories included. That
    # flag is just belt-and-suspenders though: the action always runs in an unshared network
    # namespace, so cargo can never reach out to the actual internet.
    offline = [] if spec["git"] else ["--offline"]
    subprocess.run(
        [
            Path(spec["auditable"]).resolve(),
            "auditable",
            "build",
            "--release",
            *locked,
            *offline,
        ],
        check=True,
        cwd=workspace,
        env=os.environ | {"CARGO_HOME": str(cargo_home)},
    )

    _take_binaries(workspace / "target" / "release", spec["binaries"])


if __name__ == "__main__":
    main()
