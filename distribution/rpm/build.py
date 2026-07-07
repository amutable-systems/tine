#!/usr/bin/python3
"""build_rpm — Action 4 driver: rpmbuild in an assembled buildroot.

Runs *inside* the engine root (like the install driver) under its python3. It
stages an rpmbuild %_topdir (SOURCES + the spec), mounts the stored buildroot via
`rootfs` with the topdir bound at /build, **chroots in** (like the image step
driver), and runs rpmbuild directly from the buildroot's own pinned tools — no
nested sandbox: the engine-root sandbox already provides the clean env, userns
root, and network unshare. Then it collects the produced rpms into the declared
output dir.

rpmbuild resolves every Source/Patch to %{_sourcedir}/<basename> (the URL is
reference-only), so we just drop each source into SOURCES/ under its basename.
`-ba --nocheck --noclean` defers %check and keeps the build tree.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import rootfs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="build_rpm")
    p.add_argument("--buildroot", required=True, help="assembled buildroot (tools tree)")
    p.add_argument("--spec", required=True)
    p.add_argument("--source", action="append", default=[], help="source/patch file")
    p.add_argument("--dist", default=".aos")
    p.add_argument("--source-date-epoch", type=int, required=True)
    p.add_argument("--topdir", required=True, help="scratch rpmbuild topdir (writable)")
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

    topdir = Path(args.topdir).resolve()
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
        shutil.copy(s, topdir / "SOURCES" / s.name)

    # Mount the stored buildroot and chroot in: rpmbuild execs directly from the buildroot's
    # own pinned tools, with the topdir bound at /build. Stray writes outside /build land in
    # the throwaway overlay upper. SOURCE_DATE_EPOCH must be the per-package changelog epoch,
    # overriding the fixed assembly epoch the engine-root sandbox set.
    env = os.environ | {"HOME": "/build", "SOURCE_DATE_EPOCH": str(args.source_date_epoch)}
    with rootfs.rootfs(
        "/buildroot",
        lowers=[Path(args.buildroot).resolve()],
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
            shutil.copy(f, out / f.name)
            if not f.name.endswith(".src.rpm"):
                produced[f.name] = f
    print(f"collected {len(produced)} binary rpms + srpm into {out}", file=sys.stderr)

    if args.subpackage:
        _emit_subpackages(args.subpackage, produced)

    # topdir is a declared output (its build tree feeds a later %check target). rpmbuild trees can
    # hold names buck can't store — systemd's `\x2d`-escaped unit files crash buck's path handling —
    # so rewrite topdir into buck-storable form with the same capture() the rootfs deltas use; a
    # later mount via rootfs(lowers=[topdir]) thaws the `.esc.` names back. Runs last, after the rpm
    # copies above read topdir/RPMS+SRPMS under their real names.
    rootfs.capture(topdir)
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
    # rpm auto-generates a -debuginfo per binary-bearing subpackage + a -debugsource; which
    # subpackages carry ELF can't be known when sub-targets are declared, so they aren't. Here
    # (post-build) we have the real rpms: keep them in --out but drop them from the gate, which
    # only enforces the declared %package set.
    produced = {f: p for f, p in produced.items() if not re.search(r"-debug(info|source)-", f)}
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

    # Gate on BOTH directions: a declared subpackage with no rpm, AND any
    # produced rpm matching no declared name (e.g. an unpredicted -debuginfo).
    missing = sorted(set(declared) - set(matched))
    unexpected = sorted(f for f in produced if f not in set(matched.values()))
    if missing or unexpected:
        raise SystemExit(
            "subpackage fidelity gate failed:\n"
            f"  declared but not produced: {missing}\n"
            f"  produced but not declared: {unexpected}"
        )

    for name, out_path in declared.items():
        op = Path(out_path)
        op.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(produced[matched[name]], op)
    print(f"emitted {len(declared)} subpackage sub-targets", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
