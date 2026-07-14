#!/usr/bin/python3
"""Apply ordered operations against one mounted root and capture its overlay delta."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import rootfs


def _operation(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SystemExit(f"image op is not an object with string keys: {value!r}")
    return cast(dict[str, object], value)


def _text(op: dict[str, object], field: str) -> str:
    value = op.get(field)
    if not isinstance(value, str):
        raise SystemExit(f"image op {op.get('op')!r} has invalid {field}: {value!r}")
    return value


def _run(op: dict[str, object]) -> None:
    raw_cmd = op.get("cmd")
    if not isinstance(raw_cmd, list) or not raw_cmd or not all(isinstance(arg, str) for arg in raw_cmd):
        raise SystemExit(f"image op 'run' has invalid cmd: {raw_cmd!r}")
    cmd = cast(list[str], raw_cmd)
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        raise SystemExit(f"image op `run {cmd}` failed (rc={rc})")


def _apply_filesystem(op: dict[str, object]) -> None:
    match op.get("op"):
        case "mkdir":
            dest = Path(_text(op, "path"))
            dest.mkdir(parents=True, exist_ok=True)
            if (mode := op.get("mode")) is not None:
                if not isinstance(mode, str):
                    raise SystemExit(f"image op 'mkdir' has invalid mode: {mode!r}")
                dest.chmod(int(mode, 8))
        case "symlink":
            dest = Path(_text(op, "path"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(_text(op, "target"))
        case "remove":
            dest = Path(_text(op, "path"))
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            elif dest.is_symlink() or dest.exists():
                dest.unlink()
        case kind:
            raise SystemExit(f"unknown image op {kind!r}")


def _apply(
    value: object,
    target: Path,
    installer: Path | None,
    packages_dir: Path | None,
) -> None:
    """Apply one operation with its requested view of the mounted root."""
    op = _operation(value)
    match op.get("op"):
        case "install":
            if installer is None or packages_dir is None:
                raise SystemExit("image install operation has no package installer")
            cmd = [
                str(installer),
                "--packages-dir",
                str(packages_dir),
                "--installroot",
                str(target),
            ]
            rc = subprocess.run(cmd).returncode
            if rc != 0:
                raise SystemExit(f"image package installation failed (rc={rc})")
        case "run":
            use_chroot = op.get("chroot", True)
            if not isinstance(use_chroot, bool):
                raise SystemExit(f"image op 'run' has invalid chroot: {use_chroot!r}")
            if use_chroot:
                with rootfs.chroot(target):
                    _run(op)
            else:
                _run(op)
        case "mkdir" | "symlink" | "remove":
            with rootfs.chroot(target):
                _apply_filesystem(op)
        case kind:
            raise SystemExit(f"unknown image op {kind!r}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="image")
    p.add_argument("--lower", action="append", default=[], help="ancestor delta (bottom..top)")
    p.add_argument("--out", required=True, help="this layer's delta (overlay upper)")
    p.add_argument("--work", help="throwaway overlay workdir (required with --lower)")
    p.add_argument("--installer", help="native package installer executable")
    p.add_argument("--packages-dir", help="exact package closure consumed by --installer")
    p.add_argument("--op", action="append", default=[], help="op JSON, applied in order")
    args = p.parse_args(argv)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    operations = [json.loads(raw) for raw in args.op]
    install_count = sum(_operation(operation).get("op") == "install" for operation in operations)
    if install_count > 1:
        raise SystemExit("image layer allows at most one install operation")
    if (args.installer is None) != (args.packages_dir is None):
        raise SystemExit("--installer and --packages-dir must be used together")
    if bool(install_count) != (args.installer is not None):
        raise SystemExit("image install operation requires --installer and --packages-dir")

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
            _apply(operation, target, installer, packages_dir)

    if not args.lower:
        rootfs.capture(out)
    print(f"image: applied {len(args.op)} ops over {len(args.lower)} lower(s) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
