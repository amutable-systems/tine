#!/usr/bin/python3
"""build_rpm — Action 4 driver: rpmbuild in an assembled buildroot.

Runs *inside* the engine root (like the install driver) under its python3. It
stages an rpmbuild %_topdir (SOURCES + the spec) in buck's per-action scratch dir,
overlay-merges the buildroot stack (the shared base lowerdir + this package's
BuildRequires delta) via `rootfs` with the topdir bound at /build, **chroots in**
(like the image step driver), and runs rpmbuild directly from the buildroot's
own pinned tools — no nested sandbox: the engine-root sandbox already provides the
clean env, userns root, and network unshare. Then it collects the produced rpms
into the declared output dir and deletes the build tree: only the rpms persist
(build trees are huge — a kernel's is tens of GB — and would swamp buck-out if
they were declared outputs).

rpmbuild resolves every Source/Patch to %{_sourcedir}/<basename> (the URL is
reference-only), so we just drop each source into SOURCES/ under its basename.
`-ba --nocheck --noclean` defers %check and skips rpm's own per-stage cleanup
(pointless — the whole tree is dropped at the end).
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

    # The build tree lives in buck's per-action scratch dir (forwarded by the sandbox;
    # under buck-out inside the cwd bind, so real disk, not the sandbox tmpfs). Wipe any
    # leftover from a failed prior run so a stale tree can't leak into this build.
    scratch = os.environ.get("BUCK_SCRATCH_PATH")
    if not scratch:
        raise SystemExit("BUCK_SCRATCH_PATH not set (buck provides it; the sandbox forwards it)")
    topdir = (Path(scratch) / "topdir").resolve()
    if topdir.exists():
        shutil.rmtree(topdir)
    for d in ("SOURCES", "SPECS", "BUILD", "BUILDROOT", "RPMS", "SRPMS"):
        (topdir / d).mkdir(parents=True, exist_ok=True)
    spec = Path(args.spec)
    # Freeze the release so an %autorelease spec builds without rpmautospec/git in the buildroot:
    # prepend a static %autorelease (re-applying %{?dist}) + an empty %autochangelog, overriding the
    # rpmautospec macros. Harmless for static-Release specs. Mirrors what koji does.
    frozen = (
        f"%global autorelease {args.release}%{{?dist}}\n%global autochangelog %{{nil}}\n"
    ) + spec.read_text()
    (topdir / "SPECS" / spec.name).write_text(frozen)
    for src in args.source:
        s = Path(src)
        # no hardlink: a spec scribbling on SOURCES/ must not reach the buck source artifact
        util.clone_file(s, topdir / "SOURCES" / s.name)

    # Overlay-merge the buildroot stack and chroot in: rpmbuild execs directly from the
    # buildroot's own pinned tools, with the topdir bound at /build. The merge gets an ephemeral
    # upper, so stray writes outside /build (and anything the build itself lands in the buildroot)
    # are throwaway. SOURCE_DATE_EPOCH must be the per-package changelog epoch, overriding the
    # fixed assembly epoch the engine-root sandbox set.
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
                # Make the BUILDTIME header reproducible: rpm only uses SOURCE_DATE_EPOCH
                # for the build time when this is on — it defaults off, so otherwise the
                # header gets time(NULL) (rpm build/build.cc getBuildTime). File mtimes
                # are already clamped to it by redhat-rpm-config.
                "--define", "use_source_date_epoch_as_buildtime 1",
                "-ba", "--nocheck", "--noclean",
                f"/build/SPECS/{spec.name}",
            ],
            env=env,
        ).returncode  # fmt: skip
    if rc != 0:
        return rc

    # Collect every produced rpm (all subpackages + the srpm) into --out.
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

    # Done with the build tree — drop it (it's scratch, not an output; on failure above it
    # stays behind for post-mortem and the next run wipes it).
    shutil.rmtree(topdir)
    return 0


def _emit_subpackages(pairs: list[str], produced: dict[str, Path]) -> None:
    """Map each produced rpm to its declared subpackage, gate the set, copy out.

    The fidelity gate: the declared subpackage set must equal rpmbuild's actual
    output, else the action fails. Each produced filename is matched against the
    NVRA shape `<name>-<version>-<release>.<arch>.rpm` where version and release
    are dash-free, longest declared name first so `zlib-devel` wins over `zlib`.
    The version segment is a wildcard, not the main package version: a subpackage
    may carry its own `Version:` (e.g. libbpf's `usdt-devel` at 0.1.0 vs the main
    1.7.0). Disambiguation still holds — a longer subpackage name's extra dash
    keeps its rpm from matching a shorter name's pattern. A second rpm claiming an
    already-matched name falls through to the `unexpected` set and trips the gate.
    """
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

    # Gate on BOTH directions: a declared subpackage with no rpm, AND any produced rpm matching no
    # declared name. rpm auto-generates a -debuginfo per binary-bearing subpackage plus a
    # -debugsource; which subpackages carry ELF can't be known when the sub-targets are declared, so
    # they aren't declared, and are tolerated here — but only on the produced-but-not-declared side.
    # We must not pre-drop them from `produced`: an explicitly declared package whose name merely
    # contains that substring (the kernel's kernel-debuginfo-common-<arch>) has to match a declared
    # name normally, else the gate reports it falsely missing.
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
