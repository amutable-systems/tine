"""build_rpm — Action 4 driver: rpmbuild in an assembled buildroot.

Runs *inside* the engine root (like the install driver) under its python3. It
stages an rpmbuild %_topdir (SOURCES + the spec), runs rpmbuild in
the buildroot via the shared sandbox library — a sandbox *nested* inside the
engine root one, so rpmbuild runs from the buildroot's own pinned tools — then
collects the produced rpms into the declared output dir.

rpmbuild resolves every Source/Patch to %{_sourcedir}/<basename> (the URL is
reference-only), so we just drop each source into SOURCES/ under its basename.
`-ba --nocheck --noclean` defers %check and keeps the build tree.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# The sandbox CLI is forked off as a subprocess (via sandbox.__file__) to nest a chroot
# — the buildroot's own pinned tools — for rpmbuild inside the engine-root one; the
# module is imported only to locate it in the runtree.
import sandbox


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="build_rpm")
    p.add_argument("--buildroot", required=True, help="assembled buildroot (tools tree)")
    p.add_argument("--spec", required=True)
    p.add_argument("--source", action="append", default=[], help="source/patch file")
    p.add_argument("--dist", default=".aos")
    p.add_argument("--source-date-epoch", type=int, required=True)
    p.add_argument("--topdir", required=True, help="scratch rpmbuild topdir (writable)")
    p.add_argument("--out", required=True, help="output dir to collect rpms into")
    p.add_argument("--version", required=True, help="package version (for NVR matching)")
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

    rc = subprocess.run(
        [
            sys.executable, sandbox.__file__,
            "--tools", args.buildroot,
            "--bind", f"{topdir}:/build",
            "--setenv", "HOME=/build",
            "--source-date-epoch", str(args.source_date_epoch),
            "--",
            "/usr/bin/rpmbuild",
            "--define", "_topdir /build",
            "--define", f"dist {args.dist}",
            "--define", "_buildhost reproducible",
            # Make the BUILDTIME header reproducible: rpm only uses SOURCE_DATE_EPOCH (we
            # set it via --source-date-epoch) for the build time when this is on — it
            # defaults off, so otherwise the header gets time(NULL) (rpm build/build.cc
            # getBuildTime). File mtimes are already clamped to it by redhat-rpm-config.
            "--define", "use_source_date_epoch_as_buildtime 1",
            "-ba", "--nocheck", "--noclean",
            f"/build/SPECS/{spec.name}",
        ],
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
        _emit_subpackages(args.subpackage, args.version, produced)
    return 0


def _emit_subpackages(pairs: list[str], version: str, produced: dict[str, Path]) -> None:
    """Map each produced rpm to its declared subpackage, gate the set, copy out.

    The fidelity gate: the declared subpackage set must equal rpmbuild's actual
    output, else the action fails. Each produced filename is matched against the
    exact NVRA shape `<name>-<version>-<release>.<arch>.rpm` (release has no '-'),
    longest declared name first so `zlib-devel` wins over `zlib`. A second rpm
    claiming an already-matched name falls through to the `unexpected` set and
    trips the gate.
    """
    # rpm auto-generates a -debuginfo per binary-bearing subpackage + a -debugsource; which
    # subpackages carry ELF can't be known when sub-targets are declared, so they aren't. Here
    # (post-build) we have the real rpms: keep them in --out but drop them from the gate, which
    # only enforces the declared %package set.
    produced = {f: p for f, p in produced.items() if not re.search(r"-debug(info|source)-", f)}
    declared = dict(p.split("=", 1) for p in pairs)
    names_by_len = sorted(declared, key=len, reverse=True)
    patterns = {
        name: re.compile(rf"^{re.escape(name)}-{re.escape(version)}-[^-]+\.[^.]+\.rpm$") for name in declared
    }

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
