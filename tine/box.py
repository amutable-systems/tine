"""Enter project development boxes."""

import argparse
import os
import subprocess
from pathlib import Path
from typing import NoReturn

import workspace
from errors import CliError

BOX_LABEL = "tine:box"


def add_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    box = commands.add_parser("box", help="run a command in the project development box")
    box.add_argument("--target", help="box target (discovered when omitted)")
    box.add_argument("command", nargs=argparse.REMAINDER, help="command to run after --")
    box.set_defaults(handler=run)


def _box_targets(buck: str, workspace_root: Path, cell: str) -> list[str]:
    result = subprocess.run(
        [
            buck,
            "-v",
            "0",
            "uquery",
            f"attrfilter(labels, '{BOX_LABEL}', {cell}//...)",
        ],
        check=False,
        capture_output=True,
        cwd=workspace_root,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CliError(f"could not discover box targets: {detail}")
    targets = []
    for target in result.stdout.splitlines():
        _, separator, relative = target.partition("//")
        if not separator:
            raise CliError(f"Buck returned an invalid box target: {target}")
        targets.append(f"//{relative}")
    return sorted(targets)


def _target(buck: str, workspace_root: Path, cell: str, requested: str | None) -> str:
    if requested is not None:
        return requested
    targets = _box_targets(buck, workspace_root, cell)
    if "//:box" in targets:
        return "//:box"
    if len(targets) == 1:
        return targets[0]
    if not targets:
        raise CliError(f"no box targets found in project {cell}")
    choices = "\n  ".join(targets)
    raise CliError(f"multiple box targets found; select one with --target:\n  {choices}")


def run(args: argparse.Namespace) -> NoReturn:
    command = list(args.command)
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        command = [os.environ.get("SHELL") or "/bin/bash"]

    caller = Path(args.caller_directory).resolve()
    project = workspace.require_project(caller)
    workspace_root = workspace.find_workspace(caller)
    buck = str(args.buck)
    target = _target(buck, workspace_root, project.name, args.target)
    os.chdir(caller)
    os.execv(
        buck,
        [
            buck,
            "-v",
            "0,actions",
            "run",
            "--chdir",
            str(caller),
            target,
            "--",
            *command,
        ],
    )
    raise SystemExit(127)
