#!/usr/bin/python3
"""Build RPMs inside an assembled, pinned buildroot.

Sources and the spec are staged in action scratch space, while only the produced
RPMs persist. The engine sandbox already supplies isolation around the chroot.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import rootfs
import util


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="build_rpm")
    p.add_argument(
        "--lower",
        action="append",
        default=[],
        required=True,
        metavar="LAYER",
        help="a buildroot overlay layer (bottom..top); the merged stack is the buildroot",
    )
    p.add_argument("--spec", required=True)
    p.add_argument("--source", action="append", default=[], help="source/patch file")
    p.add_argument("--dist", default=".aos")
    p.add_argument("--source-date-epoch", type=int, required=True)
    p.add_argument("--out", required=True, help="output dir to collect rpms into")
    p.add_argument("--release", required=True, help="dist-stripped Release base; freezes %autorelease")
    p.add_argument(
        "--subpackage",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="declared binary subpackage -> per-subpackage output rpm path",
    )
    args = p.parse_args(argv)

    # Use action scratch space and discard leftovers from a failed prior run.
    scratch = os.environ.get("BUCK_SCRATCH_PATH")
    if not scratch:
        raise SystemExit("BUCK_SCRATCH_PATH not set (buck provides it; the sandbox forwards it)")
    topdir = (Path(scratch) / "topdir").resolve()
    if topdir.exists():
        shutil.rmtree(topdir)
    for d in ("SOURCES", "SPECS", "BUILD", "BUILDROOT", "RPMS", "SRPMS"):
        (topdir / d).mkdir(parents=True, exist_ok=True)
    spec = Path(args.spec)
    # Freeze rpmautospec macros so builds need neither Git nor rpmautospec.
    frozen = (
        f"%global autorelease {args.release}%{{?dist}}\n%global autochangelog %{{nil}}\n"
    ) + spec.read_text()
    (topdir / "SPECS" / spec.name).write_text(frozen)
    for src in args.source:
        s = Path(src)
        # A spec may modify SOURCES, so it must not share the source artifact's inode.
        util.clone_file(s, topdir / "SOURCES" / s.name)

    # The ephemeral upper discards buildroot writes; use the package-specific epoch.
    env = os.environ | {"HOME": "/build", "SOURCE_DATE_EPOCH": str(args.source_date_epoch)}
    with rootfs.rootfs(
        "/buildroot",
        lowers=args.lower,
        binds=[(topdir, "/build")],
        apivfs=True,
        chroot=True,
    ):
        rc = subprocess.run(
            [
                "/usr/bin/rpmbuild",
                "--define", "_topdir /build",
                "--define", f"dist {args.dist}",
                "--define", "_buildhost reproducible",
                # rpm otherwise ignores SOURCE_DATE_EPOCH for the BUILDTIME header.
                "--define", "use_source_date_epoch_as_buildtime 1",
                "-ba", "--nocheck", "--noclean",
                f"/build/SPECS/{spec.name}",
            ],
            env=env,
        ).returncode  # fmt: skip
    if rc != 0:
        return rc

    # Collect binary packages and the source package.
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    produced: dict[str, Path] = {}  # basename -> path of each binary rpm
    for sub in ("RPMS", "SRPMS"):
        for f in sorted((topdir / sub).rglob("*.rpm")):
            util.clone_file(f, out / f.name, allow_link=True)
            if not f.name.endswith(".src.rpm"):
                produced[f.name] = f
    print(f"collected {len(produced)} binary rpms + srpm into {out}", file=sys.stderr)

    if args.subpackage:
        _emit_subpackages(args.subpackage, produced)

    # Preserve failed trees for diagnosis; remove successful ones.
    shutil.rmtree(topdir)
    return 0


def _emit_subpackages(pairs: list[str], produced: dict[str, Path]) -> None:
    """Match declared subpackages to output NVRA names and verify the exact set."""
    declared = dict(p.split("=", 1) for p in pairs)
    names_by_len = sorted(declared, key=len, reverse=True)
    patterns = {name: re.compile(rf"^{re.escape(name)}-[^-]+-[^-]+\.[^.]+\.rpm$") for name in declared}

    matched: dict[str, str] = {}  # subpackage name -> produced basename
    for fname in sorted(produced):
        for name in names_by_len:
            if patterns[name].match(fname):
                if name not in matched:
                    matched[name] = fname
                break

    # Tolerate auto-generated debug outputs, but still match explicitly declared debug names.
    missing = sorted(set(declared) - set(matched))
    unexpected = sorted(
        f for f in produced if f not in set(matched.values()) and not re.search(r"-debug(info|source)-", f)
    )
    if missing or unexpected:
        raise SystemExit(
            "subpackage fidelity gate failed:\n"
            f"  declared but not produced: {missing}\n"
            f"  produced but not declared: {unexpected}"
        )

    for name, out_path in declared.items():
        op = Path(out_path)
        op.parent.mkdir(parents=True, exist_ok=True)
        util.clone_file(produced[matched[name]], op, allow_link=True)
    print(f"emitted {len(declared)} subpackage sub-targets", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
