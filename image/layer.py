#!/usr/bin/python3
"""Apply ordered operations against one mounted root and capture its overlay delta."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import cast

import util

import rootfs


def _operation(value: object) -> list[object]:
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        raise SystemExit(f"image op is not a tagged array: {value!r}")
    return cast(list[object], value)


def _run(raw_cmd: object, raw_env: object) -> None:
    if not isinstance(raw_cmd, list) or not raw_cmd or not all(isinstance(arg, str) for arg in raw_cmd):
        raise SystemExit(f"image op 'run' has invalid cmd: {raw_cmd!r}")
    if not isinstance(raw_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_env.items()
    ):
        raise SystemExit(f"image op 'run' has invalid env: {raw_env!r}")
    cmd = cast(list[str], raw_cmd)
    env = cast(dict[str, str], raw_env)
    rc = subprocess.run(cmd, env=os.environ | env).returncode
    if rc != 0:
        raise SystemExit(f"image op `run {cmd}` failed (rc={rc})")


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


def _apply(
    value: object,
    target: Path,
    installer: Path | None,
    packages_dir: Path | None,
    install_langs: list[str],
    install_docs: bool,
) -> None:
    """Apply one operation with its requested view of the mounted root."""
    operation = _operation(value)
    match operation:
        case ["install", _packages]:
            if installer is None or packages_dir is None:
                raise SystemExit("image install operation has no package installer")
            cmd = [
                str(installer),
                "--packages-dir",
                str(packages_dir),
                "--installroot",
                str(target),
            ]
            for lang in install_langs:
                cmd += ["--install-langs", lang]
            if not install_docs:
                cmd.append("--no-docs")
            rc = subprocess.run(cmd).returncode
            if rc != 0:
                raise SystemExit(f"image package installation failed (rc={rc})")
        case ["run", raw_cmd, use_chroot, raw_env]:
            if not isinstance(use_chroot, bool):
                raise SystemExit(f"image op 'run' has invalid chroot: {use_chroot!r}")
            if use_chroot:
                with rootfs.chroot(target):
                    _run(raw_cmd, raw_env)
            else:
                _run(raw_cmd, raw_env)
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
    p = argparse.ArgumentParser(prog="image")
    p.add_argument("--lower", action="append", default=[], help="ancestor delta (bottom..top)")
    p.add_argument("--out", required=True, help="this layer's delta (overlay upper)")
    p.add_argument("--work", help="throwaway overlay workdir (required with --lower)")
    p.add_argument("--installer", help="native package installer executable")
    p.add_argument("--packages-dir", help="exact package closure consumed by --installer")
    p.add_argument(
        "--install-langs",
        action="append",
        default=[],
        metavar="LANG",
        help="keep only this language's translated files (repeatable)",
    )
    p.add_argument("--no-docs", action="store_true", help="skip documentation files")
    p.add_argument("--operations", required=True, help="ordered operation manifest")
    args = p.parse_args(argv)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    raw_operations = json.loads(Path(args.operations).read_text(encoding="utf-8"))
    if not isinstance(raw_operations, list):
        raise SystemExit("image operation manifest is not a list")
    operations = [_operation(operation) for operation in raw_operations]
    for operation in operations:
        if operation[0] == "copy" and len(operation) == 3 and isinstance(operation[1], str):
            operation[1] = str(Path(operation[1]).absolute())
    install_count = sum(operation[0] == "install" for operation in operations)
    if install_count > 1:
        raise SystemExit("image layer allows at most one install operation")
    if (args.installer is None) != (args.packages_dir is None):
        raise SystemExit("--installer and --packages-dir must be used together")
    if bool(install_count) != (args.installer is not None):
        raise SystemExit("image install operation requires --installer and --packages-dir")
    if args.install_langs and not install_count:
        raise SystemExit("--install-langs requires an install operation")
    if args.no_docs and not install_count:
        raise SystemExit("--no-docs requires an install operation")

    installer = Path(args.installer).resolve() if args.installer else None
    packages_dir = Path(args.packages_dir).resolve() if args.packages_dir else None
    if args.lower:
        if args.work is None:
            raise SystemExit("--lower needs a --work overlay directory")
        mounted = rootfs.rootfs(
            "/buildroot",
            lowers=args.lower,
            upperdir=out,
            workdir=Path(args.work).resolve(),
            apivfs=True,
        )
    else:
        if args.work is not None:
            raise SystemExit("--work requires --lower")
        mounted = rootfs.rootfs("/buildroot", bind=out, apivfs=True)

    with mounted as target:
        for operation in operations:
            _apply(operation, target, installer, packages_dir, args.install_langs, not args.no_docs)

    if not args.lower:
        rootfs.capture(out)
    print(f"image: applied {len(operations)} ops over {len(args.lower)} lower(s) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
