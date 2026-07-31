#!/usr/bin/python3
"""Build RPMs inside an assembled, pinned buildroot.

Sources and the spec are staged in action scratch space, while only the produced
RPMs persist. The engine sandbox already supplies isolation around the chroot.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

import specs
import util

import rootfs


class Spec(TypedDict):
    # A buildroot overlay layer stack (bottom..top); the merged stack is the buildroot.
    lower: list[str]
    spec_file: str
    sources: list[str]
    dist: str
    source_date_epoch: int
    release: str
    out: str
    # Declared binary subpackage -> its own output rpm path.
    subpackages: dict[str, str]
    rpmbuild_options: list[str]


def main(argv: list[str] | None = None) -> int:
    spec: Spec = specs.parse("build_rpm", argv)
    if not spec["lower"]:
        raise SystemExit("build_rpm: the buildroot stack cannot be empty")

    # Use action scratch space, which is what the sandbox backs /tmp with. Buck clears it before
    # each execution, so a fixed name neither collides with a preserved failed tree nor
    # accumulates across builds.
    topdir = Path("/tmp/topdir")
    for d in ("SOURCES", "SPECS", "BUILD", "BUILDROOT", "RPMS", "SRPMS"):
        (topdir / d).mkdir(parents=True)
    spec_file = Path(spec["spec_file"])
    # Freeze rpmautospec macros so builds need neither Git nor rpmautospec.
    frozen = (
        f"%global autorelease {spec['release']}%{{?dist}}\n%global autochangelog %{{nil}}\n"
    ) + spec_file.read_text()
    (topdir / "SPECS" / spec_file.name).write_text(frozen)
    for src in spec["sources"]:
        s = Path(src)
        # A spec may modify SOURCES, so it must not share the source artifact's inode.
        util.clone_file(s, topdir / "SOURCES" / s.name)

    # The ephemeral upper discards buildroot writes; use the package-specific epoch.
    env = os.environ | {"HOME": "/build", "SOURCE_DATE_EPOCH": str(spec["source_date_epoch"])}
    with rootfs.rootfs(
        "/buildroot",
        lowers=spec["lower"],
        binds=[(topdir, "/build")],
        apivfs=True,
        chroot=True,
    ):
        defines = [
            "--define", "_topdir /build",
            "--define", f"dist {spec['dist']}",
            "--define", "_buildhost reproducible",
            # rpm otherwise ignores SOURCE_DATE_EPOCH for the BUILDTIME header.
            "--define", "use_source_date_epoch_as_buildtime 1",
        ]  # fmt: skip
        rc = subprocess.run(
            [
                "/usr/bin/rpmbuild",
                *defines,
                *spec["rpmbuild_options"],
                "-ba",
                "--nocheck",
                "--noclean",
                f"/build/SPECS/{spec_file.name}",
            ],
            env=env,
        ).returncode
    if rc != 0:
        return rc

    # Collect binary packages and the source package.
    out = Path(spec["out"])
    out.mkdir(parents=True, exist_ok=True)
    produced: dict[str, Path] = {}  # basename -> path of each binary rpm
    for sub in ("RPMS", "SRPMS"):
        for f in sorted((topdir / sub).rglob("*.rpm")):
            util.clone_file(f, out / f.name, allow_link=True)
            if not f.name.endswith(".src.rpm"):
                produced[f.name] = f
    print(f"collected {len(produced)} binary rpms + srpm into {out}", file=sys.stderr)

    if spec["subpackages"]:
        _emit_subpackages(spec["subpackages"], produced)

    # Preserve failed trees for diagnosis; remove successful ones.
    shutil.rmtree(topdir)
    return 0


def _emit_subpackages(declared: dict[str, str], produced: dict[str, Path]) -> None:
    """Match declared subpackages to output NVRA names and verify the exact set."""
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
