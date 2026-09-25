# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""APT configuration and External Dependency Solver Protocol mechanics.

Named for the protocol rather than for apt, because `apt` is the module python3-apt installs
under exactly that name in the Debian box this driver runs in.
"""

import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO, NamedTuple

import deb822
import util

CAPTURE_SCENARIO = "TINE_EDSP_SCENARIO"
CAPTURE_SOLUTION = "TINE_EDSP_SOLUTION"
INTERNAL_SOLVER_PATH = Path("/usr/lib/apt/solvers/apt")
SOLVER_NAME = "tine-capture"
PROTOCOL = "EDSP 0.5"


class PackageKey(NamedTuple):
    name: str
    version: str
    arch: str


class ScenarioVersion(NamedTuple):
    """One version APT offered, and the repository it read that version from."""

    key: PackageKey
    # The `l=` of the version's `APT-Release`, which is the id the planner staged it under.
    release: str


class Solution(NamedTuple):
    installs: list[str]
    removes: list[str]


def release_label(stated: str | None) -> str:
    """The repository label an `APT-Release` names, empty where the scenario stated none.

    APT folds the field, and its continuation is the comma-separated release description the
    repository's own `Release` supplied; `l=` is the `Label:` the planner wrote there.
    """
    for described in (stated or "").replace("\n", ",").split(","):
        name, separator, value = described.strip().partition("=")
        if separator and name == "l":
            return value
    return ""


def scenario_packages(path: Path) -> dict[str, ScenarioVersion]:
    """Map APT's scenario-local IDs to the package identity and repository behind each."""
    with path.open(encoding="utf-8") as source:
        records = iter(deb822.stanzas(source))
        request = next(records, None)
        if request is None or request.get("request") != PROTOCOL:
            found = "nothing" if request is None else repr(request.get("request"))
            util.fail(f"APT requested {found}, not {PROTOCOL}")

        packages: dict[str, ScenarioVersion] = {}
        for position, stanza in enumerate(records, 1):
            what = f"EDSP package record {position}"
            apt_id = deb822.required(stanza, "apt-id", what)
            key = PackageKey(
                deb822.required(stanza, "package", what),
                deb822.required(stanza, "version", what),
                deb822.required(stanza, "architecture", what),
            )
            if apt_id in packages:
                util.fail(f"{what}: duplicate APT-ID {apt_id!r}")
            packages[apt_id] = ScenarioVersion(key, release_label(stanza.get("apt-release")))
    return packages


def read_solution(path: Path) -> Solution:
    """Read the install and remove package IDs from an EDSP answer."""
    installs: list[str] = []
    removes: list[str] = []
    with path.open(encoding="utf-8") as source:
        for stanza in deb822.stanzas(source):
            if "error" in stanza:
                message = stanza.get("message", "APT's solver reported an unspecified error")
                util.fail(f"APT dependency resolution failed: {message}")
            if "remove" in stanza:
                removes.append(stanza["remove"])
            if "install" in stanza:
                installs.append(stanza["install"])
    return Solution(installs, removes)


def proxy(
    source: BinaryIO,
    destination: BinaryIO,
    scenario: Path,
    solution: Path,
    solver: Path,
) -> int:
    """Capture an EDSP exchange while delegating unchanged to APT's own solver."""
    with scenario.open("wb") as captured:
        shutil.copyfileobj(source, captured, length=1024 * 1024)
    with scenario.open("rb") as captured, solution.open("wb") as answer:
        result = subprocess.run([solver], stdin=captured, stdout=answer, check=False)
    with solution.open("rb") as answer:
        shutil.copyfileobj(answer, destination, length=1024 * 1024)
    destination.flush()
    return result.returncode


def option(name: str, value: str | Path) -> list[str]:
    return ["-o", f"{name}={value}"]


def _nothing(scratch: Path) -> Path:
    """The empty directory every `*parts` setting names, so none of them finds the box's own."""
    nothing = scratch / "nothing.d"
    nothing.mkdir(exist_ok=True)
    return nothing


def config(scratch: Path) -> Path:
    """Write the configuration APT has to read before it reads its own.

    APT resolves `Dir::Etc::main` and `Dir::Etc::parts` while it is still loading configuration,
    so a `-o` naming them arrives after the box's `/etc/apt/apt.conf.d` has already been read and
    applied. `APT_CONFIG` is the only hook early enough to disown it, which matters most in the
    Debian box this runs in, where that directory is populated.

    Whether recommends are installed belongs here for a different reason: the solver is a process
    of APT's own, and the command line that says `--no-install-recommends` is the front end's, not
    its. What the solver reads is this file, so a request resolved anywhere else would come back
    with every recommendation of every package in the closure.
    """
    path = scratch / "apt.conf"
    path.write_text(
        'Dir::Etc::main "/dev/null";\n'
        f'Dir::Etc::parts "{_nothing(scratch)}";\n'
        'APT::Install-Recommends "false";\n',
        encoding="utf-8",
    )
    return path


def options(scratch: Path, sources: Path, preferences: Path, status: Path, arch: str) -> list[str]:
    """Build an APT configuration isolated from the caller's package state.

    What `config` already settled is not repeated here: naming it again would read as though the
    command line were what disowns the box's configuration.
    """
    state = scratch / "state"
    lists = state / "lists"
    cache = scratch / "cache"
    nothing = _nothing(scratch)
    (lists / "partial").mkdir(parents=True)
    (cache / "archives" / "partial").mkdir(parents=True)
    (scratch / "log").mkdir()
    (state / "extended_states").write_text("", encoding="utf-8")
    return [
        argument
        for setting in (
            option("Dir::Etc::sourcelist", sources),
            option("Dir::Etc::sourceparts", nothing),
            option("Dir::Etc::preferences", preferences),
            option("Dir::Etc::preferencesparts", nothing),
            option("Dir::State", state),
            option("Dir::State::status", status),
            option("Dir::State::lists", lists),
            option("Dir::Cache", cache),
            option("Dir::Cache::archives", cache / "archives"),
            # A solve writes its planner's `eipp.log.xz` even under `--simulate`, and the default
            # log directory is the box's own.
            option("Dir::Log", scratch / "log"),
            option("APT::Architecture", arch),
            option("APT::Architectures::", arch),
            # APT marks the package named `apt` essential itself, whatever the archive says, so
            # `?essential` would pull apt and its dependency chain into every root built from a
            # solve. An empty list leaves essential meaning what the archive states.
            option("pkgCacheGen::ForceEssential", ","),
            # A resolve consumes the set APT chose and orders the install itself, so APT's own
            # ordering can contribute nothing here but a failure on a bootstrap-shaped transaction.
            option("APT::Immediate-Configure", "false"),
            option("Acquire::Languages", "none"),
            option("Acquire::Check-Valid-Until", "false"),
            # APT otherwise drops to `_apt` to acquire, cannot read a scratch directory owned by
            # the user running the solve, and falls back to running as that user with a warning.
            option("APT::Sandbox::User", "root"),
            option("Debug::NoLocking", "true"),
        )
        for argument in setting
    ]


def simulated(output: str) -> set[str]:
    """The packages apt-get's own simulation says it would install.

    `Inst` is apt's own word rather than a translated one, and these lines are what the front end
    decided. The EDSP answer is what its solver reported, and the two can disagree: a downgrade is
    marked here and left out of the answer entirely, so a plan read from the answer alone drops it.
    """
    installed = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) > 1 and fields[0] == "Inst":
            # apt qualifies a name with its architecture once more than one is configured, and a
            # Debian package name never carries a colon itself.
            installed.add(fields[1].split(":", 1)[0])
    return installed


def run(arguments: Iterable[str], env: dict[str, str], what: str) -> str:
    """Run apt-get and turn its combined diagnostic stream into one failure."""
    result = subprocess.run(
        ["apt-get", *arguments],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if result.returncode:
        util.fail(f"apt-get {what} failed:\n{result.stdout.rstrip()}")
    return result.stdout
