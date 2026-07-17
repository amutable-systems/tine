"""Enter project development boxes."""

import argparse
import os
from pathlib import Path
from typing import NoReturn

import workspace


def add_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    box = commands.add_parser("box", help="run a command in the project development box")
    box.add_argument("--target", default="root//:box", help="box target (default: root//:box)")
    box.add_argument("command", nargs=argparse.REMAINDER, help="command to run after --")
    box.set_defaults(handler=run)


def run(args: argparse.Namespace) -> NoReturn:
    command = list(args.command)
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        command = [os.environ.get("SHELL") or "/bin/bash"]

    caller = Path(args.caller_directory).resolve()
    workspace.require_project(caller)
    os.environ["TINE_IN_BOX"] = "1"
    os.chdir(caller)
    buck = str(args.buck)
    os.execv(
        buck,
        [
            buck,
            "-v",
            "0,actions",
            "run",
            "--console",
            "simple",
            "--chdir",
            str(caller),
            args.target,
            "--",
            *command,
        ],
    )
    raise SystemExit(127)
