#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Resolve Debian transactions against pinned metadata with APT's EDSP interface."""

import argparse
import hashlib
import io
import os
import shlex
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple

import deb822
import edsp
import specs
import util
from util import MAGIC, decompressor

import rootfs
import transaction
from href import relative_href


class SolveSpec(transaction.Spec):
    install: list[str]
    lower: list[str]
    # The neutral solve spec also carries `cache`; Debian prebuilds no solver cache.
    cache: list[str]


class PackageRecord(NamedTuple):
    key: edsp.PackageKey
    filename: str
    size: int
    sha256: str


def read_packages(repository: transaction.Repository) -> dict[edsp.PackageKey, PackageRecord]:
    """Read the package transports APT can select from one pinned repository."""
    packages: dict[edsp.PackageKey, PackageRecord] = {}
    index = deb822.package_index(repository.path, repository.id)
    with deb822.open_text(index) as source:
        for position, stanza in enumerate(deb822.stanzas(source), 1):
            what = f"{repository.id}: Packages record {position}"
            key = edsp.PackageKey(
                deb822.required(stanza, "package", what),
                deb822.required(stanza, "version", what),
                deb822.required(stanza, "architecture", what),
            )
            filename = relative_href(
                repository.id,
                f"package {key.name} {key.version} {key.arch}",
                stanza.get("filename"),
            )
            if not filename.endswith(".deb"):
                util.fail(f"{what}: unsupported package location {filename!r}")
            record = PackageRecord(
                key,
                filename,
                deb822.integer(deb822.required(stanza, "size", what), f"{what} Size", minimum=1),
                transaction.checksum(what, deb822.required(stanza, "sha256", what)),
            )
            previous = packages.get(key)
            if previous is not None and previous != record:
                util.fail(f"{what}: duplicate package identity {key}")
            packages[key] = record
    return packages


def resolved_packages(
    repositories: list[transaction.Repository],
    scenario: Path,
    solution: Path,
    simulated: str = "",
) -> list[transaction.TransactionPackage]:
    """Join APT's selected IDs to the checksum of the repository APT read each version from."""
    identities = edsp.scenario_packages(scenario)
    answer = edsp.read_solution(solution)
    if answer.removes:
        removed = identities.get(answer.removes[0])
        package = answer.removes[0] if removed is None else removed.key.name
        util.fail(f"plan: dependency resolution would remove {package}")

    # Read an index only once something was selected from it: a solve reads a few dozen keys out
    # of indexes that run to tens of thousands of records.
    staged = {repository.id: repository for repository in repositories}
    indexes: dict[str, dict[edsp.PackageKey, PackageRecord]] = {}
    resolved: list[transaction.TransactionPackage] = []
    chosen: set[str] = set()
    for apt_id in answer.installs:
        version = identities.get(apt_id)
        if version is None:
            util.fail(f"plan: solution refers to unknown APT-ID {apt_id!r}")
        key = version.key
        # One name, version and architecture can sit in two staged repositories with different
        # bytes, and APT keeps those apart, so the repository it named is the one whose checksum
        # answers for what it chose.
        repository = staged.get(version.release)
        if repository is None:
            named = version.release or "no repository"
            util.fail(
                f"plan: APT selected {key.name} {key.version} {key.arch} from {named}, "
                "which is not one of the pinned repositories"
            )
        packages = indexes.get(repository.id)
        if packages is None:
            packages = indexes.setdefault(repository.id, read_packages(repository))
        package = packages.get(key)
        if package is None:
            util.fail(
                f"plan: APT selected {key.name} {key.version} {key.arch}, "
                f"absent from {repository.id}'s pinned Packages index"
            )
        chosen.add(key.name)
        package_id = f"{key.name}_{key.version}_{key.arch}"
        resolved.append(
            transaction.entry(
                package_id,
                repository,
                package.sha256,
                package.filename,
                package.size,
            )
        )
    dropped = sorted(edsp.simulated(simulated) - chosen)
    if dropped:
        util.fail(
            f"plan: apt-get would install {', '.join(dropped)}, which its solver did not report. "
            "A downgrade reads this way: the front end marks it and the EDSP answer omits it, so "
            "raise the priority of the repository holding the newer version instead."
        )
    print(f"plan: resolved {len(resolved)} packages", file=sys.stderr)
    return resolved


def _repository_id(value: str) -> str:
    if (
        not value
        or not value.isascii()
        or any(not (character.isalnum() or character in ".+_-") for character in value)
    ):
        util.fail(f"plan: repository id cannot be represented in an APT Release: {value!r}")
    return value


def _digest(stream: io.BufferedIOBase) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1 << 20):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def index_entries(index: Path) -> list[tuple[str, int, str]]:
    """Every name a Release has to state for APT to accept this index.

    APT keys an index target on the uncompressed name, so a Release naming only the compressed
    stream reads as a repository that does not provide `Packages` at all. It then acquires
    whichever listed compression it prefers, which is why both names describe the same content.
    """
    with index.open("rb") as raw:
        opener = decompressor(raw.read(MAGIC))
        raw.seek(0)
        if opener is None:
            digest, size = _digest(raw)
            return [(digest, size, deb822.INDEX)]
        with opener(raw) as stream:
            plain = _digest(stream)
    with index.open("rb") as raw:
        packed = _digest(raw)
    return [(*plain, deb822.INDEX), (*packed, index.name)]


def _release(repository: transaction.Repository, index: Path, arch: str) -> str:
    rid = _repository_id(repository.id)
    entries = "".join(f" {digest} {size} {name}\n" for digest, size, name in index_entries(index))
    return (
        "Origin: Tine\n"
        f"Label: {rid}\n"
        "Suite: tine\n"
        f"Codename: {rid}\n"
        "Date: Thu, 01 Jan 1970 00:00:00 UTC\n"
        f"Architectures: {arch}\n"
        "Description: Tine pinned package repository\n"
        "SHA256:\n" + entries
    )


def stage_repositories(
    repositories: list[transaction.Repository],
    scratch: Path,
    arch: str,
) -> tuple[Path, Path]:
    """Expose pinned Packages streams as labelled flat repositories with deterministic pins."""
    sources = scratch / "sources.list"
    preferences = scratch / "preferences"
    priorities = sorted({repository.priority for repository in repositories})
    source_lines: list[str] = []
    preference_stanzas: list[str] = []
    for position, repository in enumerate(repositories):
        index = deb822.package_index(repository.path, repository.id)
        staged = scratch / "repositories" / str(position)
        staged.mkdir(parents=True)
        (staged / index.name).symlink_to(index)
        (staged / "Release").write_text(_release(repository, index, arch), encoding="utf-8")
        source_lines.append(f"deb [trusted=yes] {staged.as_uri()} ./")

        # A lower Tine priority wins even across versions. Pins above 1000 intentionally permit
        # a downgrade. Repositories sharing a priority share a pin, where APT falls back to its
        # own order: the higher version first, and source order only between equal versions.
        pin = 1001 + len(priorities) - priorities.index(repository.priority)
        preference_stanzas.append(
            f"Package: *\nPin: release l={_repository_id(repository.id)}\nPin-Priority: {pin}\n"
        )
    sources.write_text("\n".join(source_lines) + "\n", encoding="utf-8")
    preferences.write_text("\n".join(preference_stanzas), encoding="utf-8")
    return sources, preferences


def write_solver(path: Path, driver: Path) -> None:
    """Publish `driver` where APT execs a solver, re-entering it the way buck ran this one.

    APT execs the file it finds under `Dir::Bin::Solvers`, so it cannot be a symlink to a driver
    buck laid out read-only, and a shebang would pick whatever python3 the box happens to have
    rather than the interpreter this process is already running under.
    """
    path.write_text(f"#!/bin/sh\nexec {shlex.join([sys.executable, str(driver)])}\n", encoding="utf-8")
    path.chmod(0o755)


def solve(
    repositories: list[transaction.Repository],
    install: list[str],
    lower: list[str],
    arch: str,
) -> list[transaction.TransactionPackage]:
    """Run a local-only APT solve and translate its captured EDSP solution.

    `arch` is Debian's name for the architecture, which everything below the caller speaks.
    """
    if not edsp.INTERNAL_SOLVER_PATH.is_file():
        util.fail(f"plan: {edsp.INTERNAL_SOLVER_PATH} is missing; install apt-utils in the solver box")

    with ExitStack() as stack:
        scratch = Path(stack.enter_context(TemporaryDirectory(prefix="deb-plan.", dir="/var/tmp")))
        root = scratch / "root"
        if lower:
            root = stack.enter_context(rootfs.rootfs("/installroot", lowers=lower))
        status = root / "var/lib/dpkg/status"
        status.parent.mkdir(parents=True, exist_ok=True)
        status.touch(exist_ok=True)

        sources, preferences = stage_repositories(repositories, scratch, arch)
        options = edsp.options(scratch, sources, preferences, status, arch)
        env = dict(os.environ)
        env["APT_CONFIG"] = str(edsp.config(scratch))
        # `Inst` is apt's own word, but the lines around it are translated, and the plan is read
        # back out of that output. Pin the locale rather than take the box's.
        env["LC_ALL"] = "C"
        edsp.run([*options, "update"], env, "update")

        solvers = scratch / "solvers"
        solvers.mkdir()
        write_solver(solvers / edsp.SOLVER_NAME, Path(sys.argv[0]).absolute())
        scenario, solution = scratch / "scenario.edsp", scratch / "solution.edsp"
        env[edsp.CAPTURE_SCENARIO] = str(scenario)
        env[edsp.CAPTURE_SOLUTION] = str(solution)
        solver_options = [
            *options,
            *edsp.option("Dir::Bin::Solvers", solvers),
            *edsp.option("APT::Solver", edsp.SOLVER_NAME),
            *edsp.option("APT::Solver::RunAsUser", "root"),
        ]
        # Debian's essential set is what every package is entitled to assume is already there, so
        # nothing declares a dependency on any of it and a closure resolved from names alone comes
        # back without a shell. Asking APT for it by pattern leaves what counts as essential to
        # the archive, which states it, rather than restating it here.
        essential = f"?essential ?architecture({arch})"
        output = edsp.run(
            [
                *solver_options,
                "--simulate",
                "--no-remove",
                "--no-install-recommends",
                "install",
                *install,
                essential,
            ],
            env,
            "solve",
        )
        if not scenario.is_file() or not solution.is_file():
            util.fail("plan: APT did not invoke the EDSP capture solver")
        resolved = resolved_packages(repositories, scenario, solution, output)
        if not resolved and not lower:
            # Resolving nothing is ordinary for a layer whose lower stack already carries what it
            # asked for. With nothing underneath it means the solve found none of it.
            util.fail(f"plan: resolved nothing for {' '.join(install)}")
        return resolved


def _proxy_invocation() -> bool:
    return edsp.CAPTURE_SCENARIO in os.environ or edsp.CAPTURE_SOLUTION in os.environ


def main(argv: list[str] | None = None) -> None:
    if _proxy_invocation():
        scenario = os.environ.get(edsp.CAPTURE_SCENARIO)
        solution = os.environ.get(edsp.CAPTURE_SOLUTION)
        if not scenario or not solution:
            util.fail("plan: incomplete EDSP capture environment")
        # Not a failure to report: APT reads this process's exit code as its solver's.
        raise SystemExit(
            edsp.proxy(
                sys.stdin.buffer,
                sys.stdout.buffer,
                Path(scenario),
                Path(solution),
                edsp.INTERNAL_SOLVER_PATH,
            )
        )

    parser = argparse.ArgumentParser(prog="plan")
    solve_parser = parser.add_subparsers(dest="command", required=True).add_parser(
        "solve",
        help="resolve the closure, writing the transaction",
    )
    specs.add_argument(solve_parser)
    solve_parser.add_argument(
        "--out",
        required=True,
        help="output transaction JSON (remote adds url/size; local adds location)",
    )
    args = parser.parse_args(argv)

    spec = specs.load(SolveSpec, args.spec, prog="plan")
    if not spec["install"]:
        util.fail("plan: a solve needs at least one install spec")
    repositories = sorted(
        transaction.load_repositories(spec, absolute=True),
        key=lambda repository: repository.priority,
    )
    resolved = solve(repositories, spec["install"], spec["lower"], spec["arch"])
    transaction.write(Path(args.out), resolved, repositories)


if __name__ == "__main__":
    main()
