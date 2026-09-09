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
from typing import TypedDict

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
    # Where the module sits inside `src`, empty when the project is its own root.
    root: str
    # The project's source tree.
    src: str
    # The build tags gating which of the project's files compile.
    tags: list[str]


def _group_by_name(listed: str) -> dict[str, list[str]]:
    """Group the main packages of a `go list -json` stream by the binary name go gives each.

    go concatenates the objects, so the stream comes apart one decode at a time. A package missing
    either field is a hard failure: go answers a misspelled name in `-json=<fields>` with empty
    objects, and a module whose every command went quiet must not read as a module with none.
    """
    packages: dict[str, list[str]] = {}
    decoder = json.JSONDecoder()
    rest = listed.lstrip()
    while rest:
        package, end = decoder.raw_decode(rest)
        rest = rest[end:].lstrip()
        if package["Name"] != "main":
            continue
        # Grouped rather than rejected on sight: two commands of the same name are only ambiguous
        # once one of them is declared, and an undeclared pair is the project's business.
        packages.setdefault(Path(package["Target"]).name, []).append(package["ImportPath"])
    return packages


def _select(binaries: list[str], packages: dict[str, list[str]], tags: list[str]) -> list[str]:
    """The packages to build, one per declared binary."""
    missing = [name for name in binaries if name not in packages]
    if missing:
        # Naming the tags because they are the one cause go stays quiet about: a directory whose
        # files they all exclude is not a package, and `go list` omits it without a word either way.
        util.fail(
            f"go-build: no main package builds {', '.join(missing)} with tags "
            f"[{' '.join(tags)}]; the module's commands are: {', '.join(sorted(packages))}"
        )
    # `go build -o <directory>` would write one of them over the other and still succeed.
    ambiguous = [name for name in binaries if len(packages[name]) > 1]
    if ambiguous:
        util.fail(
            "go-build: several main packages build "
            + "; ".join(f"{name}: {', '.join(packages[name])}" for name in ambiguous)
        )
    return sorted(packages[name][0] for name in binaries)


def _build_command(binaries: Path, packages: list[str], linker_flags: list[str]) -> list[str]:
    """The `go build` invocation producing the selected packages' binaries.

    The linker flags ride on the command line rather than in GOFLAGS, which go splits on spaces and
    which therefore cannot carry a flag whose value holds any.
    """
    cmd = ["go", "build", "-o", f"{binaries}/"]
    if linker_flags:
        cmd.append("-ldflags=" + " ".join(linker_flags))
    return cmd + packages


def _main_packages(workspace: Path, env: dict[str, str]) -> dict[str, list[str]]:
    """Every main package in the module, grouped by the binary name `go build` gives it.

    go names a binary after the last element of its package's import path, but skips a trailing
    major-version element: the v2+ module example.com/mycmd/v2 builds `mycmd`, not `v2`. The name
    here is go's own for that reason, the basename of `.Target`, which is the path `go install`
    would write the binary to. `-e` keeps a package that does not even load from failing
    the listing, so a project's unrelated commands cannot break a build ignoring them.
    """
    listed = subprocess.run(
        ["go", "list", "-e", "-json=ImportPath,Name,Target", "./..."],
        check=True,
        cwd=workspace,
        env=env,
        # Only stdout is ours; whatever go has to say goes to the action's stderr. Under -e that is
        # nothing at all, including for a directory it skipped, which is why `_select` names the tags.
        stdout=subprocess.PIPE,
        text=True,
    )
    return _group_by_name(listed.stdout)


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
        # Nothing is ever installed here. `go list` leaves .Target empty unless go has somewhere to
        # install to, and the directory it would derive from GOPATH needs a HOME the sandbox has not.
        "GOBIN": "/var/tmp/gobin",
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

    # Build only the declared binaries.
    packages = _select(list(spec["binaries"]), _main_packages(workspace, env), spec["tags"])
    subprocess.run(
        _build_command(binaries, packages, spec["linker_flags"]),
        check=True,
        cwd=workspace,
        env=env,
    )

    util.take_binaries(binaries, spec["binaries"], tool="go-build", where="the go build output")


if __name__ == "__main__":
    main()
