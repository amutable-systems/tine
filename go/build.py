#!/usr/bin/python3
"""Build a Go project from source inside a box, against its fetched module cache.

The build runs with no network: the fetched module cache is served as a file:// proxy, from which
go takes every module and re-verifies it against the committed go.sum.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict, cast

import specs
import util


class Spec(TypedDict):
    # Declared binary name -> the output to write it to.
    binaries: dict[str, str]
    # Whether to force cgo on or off, or None to leave the box toolchain's default alone.
    cgo: bool | None
    # Extra flags for the C compiler of a cgo build, on top of the -O2 -g always passed.
    cgo_cflags: list[str]
    # go's build cache, kept from the previous build of this project.
    # None outside an incremental build.
    gocache: str | None
    # Flags for the Go linker, passed as -ldflags.
    linker_flags: list[str]
    # The fetched module cache directory, or None for a project without a go.sum.
    module_cache_dir: str | None
    # Output name -> Go package selector.
    packages: dict[str, str]
    # Where the module sits inside `src`, empty when the project is its own root.
    root: str
    # The project's source tree.
    src: str
    # The build tags gating which of the project's files compile.
    tags: list[str]


def _package(selector: str, workspace: Path, env: dict[str, str]) -> str:
    """Resolve a selector to exactly one main package before building an executable."""
    if not selector or selector.startswith("-") or selector.endswith(".go"):
        util.fail(f"go-build: invalid package selection {selector!r}")
    listed = subprocess.run(
        ["go", "list", "-json=ImportPath,Name", selector],
        check=True,
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    error = f"go-build: package selection {selector!r} must resolve to exactly one main package"
    try:
        package = cast(dict[str, str], json.loads(listed.stdout))
    except json.JSONDecodeError:
        util.fail(error)
    if package["Name"] != "main":
        util.fail(error)
    return package["ImportPath"]


def _build_command(binary: Path, package: str, linker_flags: list[str]) -> list[str]:
    """Build one package to its declared output name.

    The linker flags ride on the command line rather than in GOFLAGS, which go splits on spaces and
    which therefore cannot carry a flag whose value holds any.
    """
    cmd = ["go", "build", "-o", str(binary)]
    if linker_flags:
        cmd.append("-ldflags=" + " ".join(linker_flags))
    return cmd + [package]


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "go-build", argv)

    # Built in a copy so that nothing go does can land in `src`, which is an output artifact of
    # this same target: -mod=readonly should keep go out of the tree, but a build tree is not the
    # place to rely on "should". The sandbox backs /var/tmp with the action's scratch space.
    build = Path("/var/tmp/build")
    binaries = Path("/var/tmp/binaries")
    shutil.copytree(spec["src"], build)
    binaries.mkdir()

    gocache = Path(spec["gocache"] or "/var/tmp/gocache").resolve()
    gocache.mkdir(parents=True, exist_ok=True)

    if spec["module_cache_dir"] is None:
        # Nothing was fetched; anything go would want to resolve is a hard, named failure.
        proxy = "off"
    else:
        # The download half of a module cache is exactly the layout a file:// proxy serves. go
        # re-extracts each module from it and re-checks the committed go.sum, so the fetch action's
        # output is verified again right here.
        proxy = "file://{}/cache/download".format(Path(spec["module_cache_dir"]).resolve())

    workspace = build / spec["root"]
    # Carried in GOFLAGS rather than on each command line, so that the listing below and the build
    # cannot end up disagreeing about which of the project's files are in the module at all.
    # -trimpath and -buildvcs=false are for reproducibility: no absolute paths in the binaries, no
    # VCS stamp from whatever the checkout happens to carry. -mod=readonly refuses to touch
    # go.mod/go.sum, making a stale pin a build failure rather than a silent re-resolution.
    # -modcacherw so that buck can clean the scratch space.
    flags = ["-buildvcs=false", "-mod=readonly", "-modcacherw", "-trimpath"]
    if spec["tags"]:
        flags.append("-tags=" + ",".join(spec["tags"]))
    env = os.environ | {
        # Spelled out rather than left to the box's `go env` defaults, so a project's extra
        # flags add to a known baseline instead of replacing whatever go would have used.
        "CGO_CFLAGS": " ".join(["-O2", "-g", *spec["cgo_cflags"]]),
        "GOCACHE": str(gocache),
        "GOENV": "off",
        "GOFLAGS": " ".join(flags),
        "GOMODCACHE": "/var/tmp/modules",
        "GOPROXY": proxy,
        # The committed go.sum is the sole trust anchor here: with -mod=readonly a module it does
        # not pin is a build failure rather than something to look up, and a database is not
        # reachable offline anyway.
        "GOSUMDB": "off",
        "GOTOOLCHAIN": "local",
        # A go.work above the module root switches go into workspace mode, where a go.work.sum
        # replaces the go.sum this build is pinned by. The rule refuses one among the sources, but
        # only this covers one outside the checkout entirely.
        "GOWORK": "off",
    }
    if spec["cgo"] is not None:
        # Left alone otherwise, so that the box's toolchain decides as it would for a `go build`
        # run in the checkout by hand. Note that its default is on: importing net or os/user is
        # enough to need a C compiler in the box and to link the binary dynamically.
        env["CGO_ENABLED"] = "1" if spec["cgo"] else "0"

    for name, selector in spec["packages"].items():
        package = _package(selector, workspace, env)
        subprocess.run(
            _build_command(binaries / name, package, spec["linker_flags"]),
            check=True,
            cwd=workspace,
            env=env,
        )

    util.take_binaries(binaries, spec["binaries"], tool="go-build", where="the go build output")


if __name__ == "__main__":
    main()
