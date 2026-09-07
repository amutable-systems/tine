#!/usr/bin/python3
"""Build RPMs inside an assembled, pinned buildroot.

Sources and the RPM spec are staged in action scratch space. Buck keeps the
build directory between runs when the project's dev configuration requests it;
otherwise only the produced RPMs persist. The box sandbox already supplies
isolation around the chroot.
"""

import os
import re
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import TypedDict

import specs
import util

import rootfs

# A persistent build directory marks a local iteration build; skip the release-only costs of
# optimization, debug packaging, and payload compression.
_INCREMENTAL_RPMBUILD_OPTIONS = [
    "--without", "lto",
    "--undefine", "_lto_cflags",
    "--undefine", "_annotated_build",
    "--define", "debug_package %{nil}",
    "--define", "_binary_payload w.ufdio",
    "--define", "_source_payload w.ufdio",
]  # fmt: skip


class Spec(TypedDict):
    """Buck's generated build invocation, distinct from the package's RPM spec."""

    # A build directory that survives between runs, or None for a clean build.
    build_dir: str | None
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
    # Extra switches for a build against `source_tree`, in addition to the common ones below.
    in_place_rpmbuild_options: list[str]
    rpmbuild_options: list[str]
    # A prepared source tree to build in place, or None to unpack the declared sources through %prep.
    source_tree: str | None


def build_rpm(spec: Spec, topdir: Path = Path("/var/tmp/topdir")) -> int:
    """Build the RPMs described by `spec` in action scratch space."""
    with rootfs.capture_on_exit(topdir):
        rc = _build_rpm(spec, topdir)
    if rc == 0:
        shutil.rmtree(topdir)
    return rc


def _build_rpm(spec: Spec, topdir: Path) -> int:
    if not spec["lower"]:
        util.fail("build_rpm: the buildroot stack cannot be empty")

    # Use action scratch space, which is what the sandbox backs /var/tmp with. Buck clears it
    # before each execution, so a fixed name neither collides with a preserved failed tree nor
    # accumulates across builds.
    for d in ("SOURCES", "SPECS", "BUILD", "BUILDROOT", "RPMS", "SRPMS"):
        (topdir / d).mkdir(parents=True)
    source_tree = spec["source_tree"]
    if source_tree is not None:
        # Inputs are immutable artifacts, while build-in-place projects routinely generate files in
        # their checkout. Keep this writable copy separate from RPM's SOURCES archives and patches.
        # Keep symlinks as symlinks: a tree may link a directory to its own parent, and following that
        # copies it into itself until the path length runs out.
        shutil.copytree(source_tree, topdir / "CHECKOUT", symlinks=True)

    spec_file = Path(spec["spec_file"])
    if source_tree is not None and spec_file.is_relative_to(source_tree):
        relative_spec = spec_file.relative_to(source_tree)
        if ".." in relative_spec.parts:
            util.fail(f"build_rpm: RPM spec must stay within the source tree: {relative_spec}")
        staged_spec = topdir / "CHECKOUT" / relative_spec
        # Freezing runs before chroot: an escaping spec symlink must not write into the checkout
        # through another path in the outer sandbox.
        if not staged_spec.resolve().is_relative_to((topdir / "CHECKOUT").resolve()):
            util.fail(f"build_rpm: RPM spec symlink escapes the source tree: {relative_spec}")
        source_spec = staged_spec
        chroot_spec = Path("/build/CHECKOUT") / relative_spec
        sourcedir = chroot_spec.parent
    else:
        source_spec = spec_file
        staged_spec = topdir / "SPECS" / spec_file.name
        chroot_spec = Path("/build/SPECS") / spec_file.name
        sourcedir = None
        for src in spec["sources"]:
            s = Path(src)
            # A spec may modify SOURCES, so it must not share the source artifact's inode.
            util.clone_file(s, topdir / "SOURCES" / s.name)

    if not source_spec.is_file():
        util.fail(f"build_rpm: RPM spec does not exist: {spec_file}")

    # Freeze rpmautospec macros so builds need neither Git nor rpmautospec. Keep an in-place spec beside
    # its auxiliary files in the disposable tree, preserving relative includes as well.
    frozen = (
        f"%global autorelease {spec['release']}%{{?dist}}\n%global autochangelog %{{nil}}\n"
    ) + source_spec.read_text()
    staged_spec.write_text(frozen)

    binds = [(topdir, "/build")]
    build_dir: Path | None = None
    if spec["build_dir"] is not None:
        # Buck supplies a project-relative output. Anchor it in the host namespace because mount(2)
        # resolves the bind source before rootfs enters the buildroot.
        build_dir = Path(spec["build_dir"]).absolute()
        build_dir.mkdir(parents=True, exist_ok=True)
        binds.append((build_dir, "/build/BUILD"))

    # The ephemeral upper discards buildroot writes; use the package-specific epoch.
    env = os.environ | {"HOME": "/build", "SOURCE_DATE_EPOCH": str(spec["source_date_epoch"])}
    # Both scratch and persistent outputs need to stay removable by Buck after failed builds.
    # Capture after unmounting so package permissions remain intact while rpmbuild uses them.
    with (
        rootfs.capture_on_exit(build_dir) if build_dir is not None else nullcontext(),
        rootfs.rootfs(
            "/buildroot",
            lowers=spec["lower"],
            binds=binds,
            apivfs=True,
            chroot=True,
        ),
    ):
        defines = [
            "--define", "_topdir /build",
            "--define", f"dist {spec['dist']}",
            "--define", "_buildhost reproducible",
            # rpm otherwise ignores SOURCE_DATE_EPOCH for the BUILDTIME header.
            "--define", "use_source_date_epoch_as_buildtime 1",
        ]  # fmt: skip
        if sourcedir is not None:
            defines += ["--define", f"_sourcedir {sourcedir}"]
        if build_dir is not None:
            defines += ["--define", "_vpath_builddir /build/BUILD"]
        mode = ["-ba"]
        options = spec["rpmbuild_options"] + (_INCREMENTAL_RPMBUILD_OPTIONS if build_dir is not None else [])
        cwd = None
        if source_tree is not None:
            mode = ["-bb", "--noprep", "--build-in-place"]
            options += spec["in_place_rpmbuild_options"]
            cwd = Path("/build/CHECKOUT")
        rc = subprocess.run(
            [
                "/usr/bin/rpmbuild",
                *defines,
                *options,
                *mode,
                "--nocheck",
                "--noclean",
                str(chroot_spec),
            ],
            cwd=cwd,
            env=env,
        ).returncode
    if rc != 0:
        return rc

    # Collect binary packages and, for a regular archive build, the source package.
    out = Path(spec["out"])
    out.mkdir(parents=True, exist_ok=True)
    # Buck keeps every output of an incremental action, not only its private build directory.
    for previous in out.iterdir():
        previous.unlink()
    produced: dict[str, Path] = {}  # basename -> path of each binary rpm
    for sub in ("RPMS", "SRPMS"):
        for f in sorted((topdir / sub).rglob("*.rpm")):
            util.clone_file(f, out / f.name, allow_link=True)
            if not f.name.endswith(".src.rpm"):
                produced[f.name] = f
    source_output = "" if source_tree is not None else " + srpm"
    print(f"collected {len(produced)} binary rpms{source_output} into {out}", file=sys.stderr)

    if spec["subpackages"]:
        _emit_subpackages(spec["subpackages"], produced)

    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse an invocation and build its RPMs."""
    return build_rpm(specs.parse(Spec, "build_rpm", argv))


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
        util.fail(
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
