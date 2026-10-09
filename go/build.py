#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build a Go project from source inside a box, against its fetched module cache.

The build runs with no network: the fetched module cache is served as a file:// proxy, from which
go takes every module and re-verifies it against the committed go.sum.
"""

import json
import os
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any, TypedDict, cast

import specs
import util

import rootfs


class Spec(TypedDict):
    # The output directory holding one binary per package.
    bin: str
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
    # Where the module sits inside src, empty when the project is its own root.
    root: str
    # The project's source directory artifact.
    src: str
    # The build tags gating which of the project's files compile.
    tags: list[str]
    # The Go package patterns whose tests to compile, or None to compile no tests.
    test_packages: list[str] | None
    # The output directory for the test binaries and their manifest, or None to compile no tests.
    tests: str | None


class Test(TypedDict):
    # The test binary's file name in the tests directory.
    binary: str
    # The package's import path.
    package: str
    # The package's directory relative to src. go test runs a test binary in this directory.
    dir: str


def _list(patterns: list[str], fields: str, workspace: Path, env: dict[str, str]) -> list[dict[str, Any]]:
    """Return the packages that `patterns` match, as go list reports them."""
    for pattern in patterns:
        if not pattern or pattern.startswith("-") or pattern.endswith(".go"):
            util.fail(f"go-build: invalid package selection {pattern!r}")
    listed = subprocess.run(
        ["go", "list", f"-json={fields}", *patterns],
        check=True,
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    # go prints one object per package, back to back.
    decoder = json.JSONDecoder()
    packages: list[dict[str, Any]] = []
    rest = listed.stdout.lstrip()
    while rest:
        package, end = cast(tuple[dict[str, Any], int], decoder.raw_decode(rest))
        packages.append(package)
        rest = rest[end:].lstrip()
    return packages


def _package(selector: str, workspace: Path, env: dict[str, str]) -> str:
    """Resolve a selector to exactly one main package before building an executable."""
    # A pattern such as the `./...` an undeclared `packages` stands for also matches the libraries
    # beside the program, which are not candidates.
    mains = [
        package["ImportPath"]
        for package in _list([selector], "ImportPath,Name", workspace, env)
        if package["Name"] == "main"
    ]
    if len(mains) != 1:
        util.fail(
            f"go-build: package selection {selector!r} must resolve to exactly one main package, "
            f"found {mains}"
        )
    return cast(str, mains[0])


def _test_packages(
    patterns: list[str], workspace: Path, src: Path, env: dict[str, str]
) -> list[tuple[str, str]]:
    """Return the import path and the directory relative to `src` of each matched package that has tests."""
    found: list[tuple[str, str]] = []
    fields = "ImportPath,Dir,TestGoFiles,XTestGoFiles"
    for package in _list(patterns, fields, workspace, env):
        if not package.get("TestGoFiles") and not package.get("XTestGoFiles"):
            continue
        # A pattern can name a package of a dependency, which go extracts into the module cache in
        # the scratch space. go.test runs each test binary in its package directory, and that
        # directory does not exist when the tests run.
        directory = Path(package["Dir"]).resolve()
        if not directory.is_relative_to(src.resolve()):
            util.fail(f"go-build: test package {package['ImportPath']} is not in src")
        found.append((package["ImportPath"], str(directory.relative_to(src.resolve()))))
    return found


def _test_command(binary: Path, package: str, linker_flags: list[str]) -> list[str]:
    """Compile the tests of one package into a test binary."""
    # go test runs a subset of go vet before it compiles the tests, also with -c. A vet finding would
    # fail the build of the project's binaries along with the tests.
    cmd = ["go", "test", "-c", "-vet=off", "-o", str(binary)]
    if linker_flags:
        cmd.append("-ldflags=" + " ".join(linker_flags))
    return cmd + [package]


def _build_command(binary: Path, package: str, linker_flags: list[str]) -> list[str]:
    """Build one package to its declared output name.

    The linker flags ride on the command line rather than in GOFLAGS, which go splits on spaces and
    which therefore cannot carry a flag whose value holds any.
    """
    cmd = ["go", "build", "-o", str(binary)]
    if linker_flags:
        cmd.append("-ldflags=" + " ".join(linker_flags))
    return cmd + [package]


def build_go(spec: Spec) -> None:
    """Build a Go module in its source directory, in a read-only project."""
    # The sandbox points TMPDIR at /var/tmp, which it backs with the action's scratch space.
    scratch = Path(tempfile.gettempdir())
    out = scratch / "bin"

    gocache = scratch / "gocache"
    gocache.mkdir()

    if spec["module_cache_dir"] is None:
        # Nothing was fetched; anything go would want to resolve is a hard, named failure.
        proxy = "off"
    else:
        # The download half of a module cache is exactly the layout a file:// proxy serves. go
        # re-extracts each module from it and re-checks the committed go.sum, so the fetch action's
        # output is verified again right here.
        proxy = "file://{}/cache/download".format(Path(spec["module_cache_dir"]).resolve())

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
        "GOMODCACHE": str(scratch / "modules"),
        "GOPROXY": proxy,
        # The committed go.sum is the sole trust anchor here: with -mod=readonly a module it does
        # not pin is a build failure rather than something to look up, and a database is not
        # reachable offline anyway.
        "GOSUMDB": "off",
        "GOTOOLCHAIN": "local",
        # A go.work above the module root switches go into workspace mode, where a go.work.sum
        # replaces the go.sum this build is pinned by.
        "GOWORK": "off",
    }
    if spec["cgo"] is not None:
        # Left alone otherwise, so that the box's toolchain decides as it would for a `go build`
        # run in the checkout by hand. Note that its default is on: importing net or os/user is
        # enough to need a C compiler in the box and to link the binary dynamically.
        env["CGO_ENABLED"] = "1" if spec["cgo"] else "0"

    tests = scratch / "tests"

    with ExitStack() as stack:
        outputs = {Path(spec["bin"]): out}
        if spec["gocache"] is not None:
            outputs[Path(spec["gocache"])] = gocache
        if spec["tests"] is not None:
            outputs[Path(spec["tests"])] = tests
        stack.enter_context(rootfs.readonly_project(Path.cwd(), outputs))
        util.remove_previous_binaries(out)

        workspace = Path(spec["src"]) / spec["root"]
        for name, selector in spec["packages"].items():
            package = _package(selector, workspace, env)
            subprocess.run(
                _build_command(out / name, package, spec["linker_flags"]),
                check=True,
                cwd=workspace,
                env=env,
            )

        if spec["tests"] is None or spec["test_packages"] is None:
            return
        util.remove_previous_binaries(tests)
        # The tests compile in this action because it already has go's build cache filled with the
        # project's packages. A separate action would compile the packages again.
        manifest: list[Test] = []
        for index, (package, directory) in enumerate(
            _test_packages(spec["test_packages"], workspace, Path(spec["src"]), env)
        ):
            # Two packages can have the same name, so the binaries are numbered.
            binary = f"{index}.test"
            subprocess.run(
                _test_command(tests / binary, package, spec["linker_flags"]),
                check=True,
                cwd=workspace,
                env=env,
            )
            manifest.append(Test(binary=binary, package=package, dir=directory))
        (tests / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    build_go(specs.parse(Spec, "go-build", argv))


if __name__ == "__main__":
    main()
