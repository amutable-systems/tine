"""The tine command-line entry point."""

import argparse
import sys
from pathlib import Path

import box
import workspace
from errors import CliError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tine")
    parser.add_argument("--buck", type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument("--source-root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    parser.add_argument("--caller-directory", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    box.add_command(commands)
    workspace.add_command(commands)
    return parser


def main() -> None:
    args = _parser().parse_args()
    args.buck = args.buck.resolve()
    args.source_root = args.source_root.resolve()
    args.caller_directory = args.caller_directory.resolve()
    try:
        args.handler(args)
    except CliError as error:
        sys.exit(f"tine: {error}")


if __name__ == "__main__":
    main()
