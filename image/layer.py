#!/usr/bin/python3
"""Apply ordered operations against one mounted root and capture its overlay delta."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import TypedDict, cast

import specs
import util

import rootfs


class InstallSpec(TypedDict):
    installer: str
    packages_dir: str
    langs: list[str]
    docs: bool


class Spec(TypedDict):
    lower: list[str]
    out: str
    work: str | None
    install: InstallSpec | None
    operations: list[object]


# Where a chrooted command sees the project, and so every declared input. /run is one of the
# tmpfs mounts apivfs puts over the mounted root, so the mount point never reaches the overlay
# upper the layer captures, and the path does not depend on where the project is checked out.
PROJECT = "/run/tine/project"


def _chroots(operation: list[object]) -> bool:
    return operation[0] == "run" and len(operation) == 4 and operation[3] is True


def _operation(value: object) -> list[object]:
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        raise SystemExit(f"image op is not a tagged array: {value!r}")
    return cast(list[object], value)


def _run(tag: str, raw_cmd: object, raw_env: object, cwd: str | None = None) -> None:
    if not isinstance(raw_cmd, list) or not raw_cmd or not all(isinstance(arg, str) for arg in raw_cmd):
        raise SystemExit(f"image op {tag!r} has invalid cmd: {raw_cmd!r}")
    if not isinstance(raw_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_env.items()
    ):
        raise SystemExit(f"image op {tag!r} has invalid env: {raw_env!r}")
    cmd = cast(list[str], raw_cmd)
    env = cast(dict[str, str], raw_env)
    rc = subprocess.run(cmd, env=os.environ | env, cwd=cwd).returncode
    if rc != 0:
        raise SystemExit(f"image op `{tag} {cmd}` failed (rc={rc})")


def _destination(tree: Path, value: str) -> Path:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"image copy destination must be an absolute image path: {value!r}")
    return tree.joinpath(*path.parts[1:])


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            raise SystemExit(f"cannot replace directory {destination} with symlink {source}")
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        shutil.copy2(source, destination, follow_symlinks=False)
    elif source.is_dir():
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise SystemExit(f"cannot merge directory {source} into non-directory {destination}")
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            # Reflink each regular file; copytree recreates symlinks itself
            copy_function=lambda s, d: util.clone_file(Path(s), Path(d)),
            symlinks=True,
        )
    else:
        if destination.is_dir() and not destination.is_symlink():
            raise SystemExit(f"cannot replace directory {destination} with file {source}")
        if destination.is_symlink():
            destination.unlink()
        util.clone_file(source, destination)


def _merge_os_release(tree: Path, raw_fields: object) -> None:
    """Merge quoted KEY="value" assignments into /usr/lib/os-release, replacing existing keys."""
    if not isinstance(raw_fields, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_fields.items()
    ):
        raise SystemExit(f"image op 'os_release' has invalid fields: {raw_fields!r}")
    fields = dict(cast(dict[str, str], raw_fields))
    for key, value in fields.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise SystemExit(f"image op 'os_release' has invalid key: {key!r}")
        # Values are emitted double-quoted verbatim, so refuse anything needing escapes.
        if '"' in value or "\\" in value or "\n" in value:
            raise SystemExit(f"image op 'os_release' value needs escaping: {value!r}")
    path = tree / "usr/lib/os-release"
    lines = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, _ = line.partition("=")
            if sep and key in fields:
                line = f'{key}="{fields.pop(key)}"'
            lines.append(line)
    lines += [f'{key}="{value}"' for key, value in fields.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _apply_filesystem(operation: list[object]) -> None:
    match operation:
        case ["mkdir", str(path), mode]:
            dest = Path(path)
            dest.mkdir(parents=True, exist_ok=True)
            if mode is not None:
                if not isinstance(mode, str):
                    raise SystemExit(f"image op 'mkdir' has invalid mode: {mode!r}")
                dest.chmod(int(mode, 8))
        case ["symlink", str(target), str(path)]:
            dest = Path(path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(target)
        case ["remove", str(path)]:
            util.remove_path(Path(path))
        case _:
            raise SystemExit(f"invalid image filesystem op: {operation!r}")


def _install(install: InstallSpec, target: Path, scratch: Path) -> None:
    """Install the layer's package closure into the mounted root."""
    spec = specs.write(
        scratch / "install.spec.json",
        {
            "packages_dir": str(Path(install["packages_dir"]).resolve()),
            "target": None,
            "installroot": str(target),
            "lower": [],
            "work": None,
            "engine_config": False,
            "langs": install["langs"],
            "docs": install["docs"],
        },
    )
    rc = subprocess.run([str(Path(install["installer"]).resolve()), "--spec", str(spec)]).returncode
    if rc != 0:
        raise SystemExit(f"image package installation failed (rc={rc})")


def _apply(
    value: object,
    target: Path,
    install: InstallSpec | None,
    scratch: Path,
) -> None:
    """Apply one operation with its requested view of the mounted root."""
    operation = _operation(value)
    match operation:
        case ["install", _packages]:
            if install is None:
                raise SystemExit("image install operation has no package installer")
            _install(install, target, scratch)
        case ["run", raw_cmd, raw_env, bool(chroot)]:
            if not chroot:
                _run("run", raw_cmd, raw_env)
            else:
                with rootfs.chroot(target):
                    _run("run", raw_cmd, raw_env, cwd=PROJECT)
        case ["copy", str(source), str(destination)]:
            _copy(Path(source), _destination(target, destination))
        case ["os_release", raw_fields]:
            _merge_os_release(target, raw_fields)
        case ["mkdir", _, _] | ["symlink", _, _] | ["remove", _]:
            with rootfs.chroot(target):
                _apply_filesystem(operation)
        case _:
            raise SystemExit(f"invalid image op: {operation!r}")


def main(argv: list[str] | None = None) -> None:
    spec: Spec = specs.parse("image", argv)

    out = Path(spec["out"]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    operations = [_operation(operation) for operation in spec["operations"]]
    for operation in operations:
        if operation[0] == "copy" and len(operation) == 3 and isinstance(operation[1], str):
            operation[1] = str(Path(operation[1]).absolute())
    install = spec["install"]
    install_count = sum(operation[0] == "install" for operation in operations)
    if install_count > 1:
        raise SystemExit("image layer allows at most one install operation")
    if bool(install_count) != (install is not None):
        raise SystemExit("image install operation and package installer require each other")

    # A chrooted command names an artifact exactly as an engine command does, so the project is
    # mounted for the whole layer whenever one asks for it.
    binds = [(os.getcwd(), PROJECT)] if any(_chroots(op) for op in operations) else []

    lower = spec["lower"]
    if lower:
        if spec["work"] is None:
            raise SystemExit("image lower stack needs a work overlay directory")
        mounted = rootfs.rootfs(
            "/buildroot",
            lowers=lower,
            upperdir=out,
            workdir=Path(spec["work"]).resolve(),
            apivfs=True,
            binds=binds,
        )
    else:
        if spec["work"] is not None:
            raise SystemExit("image work overlay directory requires a lower stack")
        mounted = rootfs.rootfs("/buildroot", bind=out, apivfs=True, binds=binds)

    with mounted as target, tempfile.TemporaryDirectory(prefix="layer.") as scratch:
        for operation in operations:
            _apply(operation, target, install, Path(scratch))

    if not lower:
        rootfs.capture(out)
    print(f"image: applied {len(operations)} ops over {len(lower)} lower(s) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
