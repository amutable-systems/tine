# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Run apt-get isolated from the box it runs in.

The module is named `aptget` because python3-apt already installs a module named `apt`.
"""

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

import util


def option(name: str, value: str | Path) -> list[str]:
    return ["-o", f"{name}={value}"]


def _nothing(scratch: Path) -> Path:
    """Return an empty directory for the `*parts` settings, so that APT reads no directory of the box."""
    nothing = scratch / "nothing.d"
    nothing.mkdir(exist_ok=True)
    return nothing


def environment(scratch: Path) -> dict[str, str]:
    """Return an environment with an `APT_CONFIG` that keeps APT from reading the box's configuration.

    APT resolves `Dir::Etc::main` and `Dir::Etc::parts` while it loads its configuration. A `-o`
    option for either setting takes effect after APT has read `/etc/apt/apt.conf.d` of the box. APT
    reads the file that `APT_CONFIG` names before that. The Debian box has files in that directory.
    """
    path = scratch / "apt.conf"
    path.write_text(
        f'Dir::Etc::main "/dev/null";\nDir::Etc::parts "{_nothing(scratch)}";\n',
        encoding="utf-8",
    )
    return {**os.environ, "APT_CONFIG": str(path)}


def options(scratch: Path, status: Path, arch: str) -> list[str]:
    """Return the options that isolate APT from the package state of the box, with no repository.

    `Dir::Etc::main` and `Dir::Etc::parts` are missing on purpose. `environment()` sets them, and an
    option on the command line would take effect too late.
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
            option("Dir::Etc::sourcelist", "/dev/null"),
            option("Dir::Etc::sourceparts", nothing),
            option("Dir::Etc::preferences", "/dev/null"),
            option("Dir::Etc::preferencesparts", nothing),
            option("Dir::State", state),
            option("Dir::State::status", status),
            option("Dir::State::lists", lists),
            option("Dir::Cache", cache),
            option("Dir::Cache::archives", cache / "archives"),
            # APT would otherwise write its logs to `/var/log/apt` of the box.
            option("Dir::Log", scratch / "log"),
            option("APT::Architecture", arch),
            option("APT::Architectures::", arch),
            # APT treats the package `apt` as essential even though the archive does not mark it.
            # The `?essential` pattern in `plan.py` would then install apt and its dependencies
            # into every root. An empty list makes APT use only the `Essential` field of the archive.
            option("pkgCacheGen::ForceEssential", ","),
            # By default, APT configures an essential package right after it unpacks the package.
            # That fails when the transaction installs the whole essential set into a fresh root.
            # `install.lay_down()` has extracted every package by then, so the order does not matter.
            option("APT::Immediate-Configure", "false"),
            option("APT::Install-Recommends", "false"),
            option("Acquire::Languages", "none"),
            # APT otherwise downloads as the user `_apt`. That user cannot read the scratch
            # directory, so APT prints a warning and downloads as the calling user.
            option("APT::Sandbox::User", "root"),
            option("Debug::NoLocking", "true"),
        )
        for argument in setting
    ]


def run(arguments: Iterable[str], env: dict[str, str], what: str, *, capture: bool = True) -> str:
    """Run apt-get and return its output, which a failure includes in its message.

    With `capture=False`, apt-get writes to the streams of the caller and the result is empty.
    """
    result = subprocess.run(
        ["apt-get", *arguments],
        env=env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
        check=False,
    )
    if result.returncode:
        util.fail(f"apt-get {what} failed" + (f":\n{result.stdout.rstrip()}" if capture else ""))
    return result.stdout if capture else ""
