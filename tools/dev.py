"""Run pinned source checks and formatters against the active tine cell."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _bold(label: str) -> None:
    print(f"\033[1m{label}\033[0m", flush=True)


def _run(cmd: list[str | Path], **kwargs) -> None:
    # Tools print their own diagnostics; propagate failures without a traceback.
    proc = subprocess.run(cmd, **kwargs)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def _buck_out(buck: str, *args: str) -> str:
    proc = subprocess.run([buck, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        # Relay Buck's diagnostic before exiting.
        print(proc.stderr, end="", file=sys.stderr, flush=True)
        raise SystemExit(proc.returncode)
    return proc.stdout.strip()


def _cell_root(buck: str, cell: str) -> Path:
    return Path(_buck_out(buck, "audit", "cell", cell, "--paths-only"))


def _starlark_srcs(buck: str) -> list[Path]:
    # Check every loaded in-tree Starlark file; ignore dead files, external cells, and JSON.
    cells = json.loads(_buck_out(buck, "audit", "cell", "--json"))
    roots = {path: name for name, path in sorted(cells.items()) if name not in ("none", "prelude")}
    universe = " + ".join(sorted(f"{name}//..." for name in roots.values()))
    project = Path(_buck_out(buck, "root", "--kind", "project"))
    files = sorted(
        project / f for f in _buck_out(buck, "uquery", f"allbuildfiles({universe})").splitlines() if f
    )
    return [f for f in files if f.is_relative_to(Path(cells["tine"])) and f.suffix != ".json"]


def _check(args: argparse.Namespace) -> None:
    cell = _cell_root(args.buck, "tine")
    _bold("ruff")
    _run([args.ruff, "format", "--check", "--no-cache", cell])
    _run([args.ruff, "check", "--no-cache", cell])
    _bold("ty")
    # Resolve third-party imports from the pinned engine runtime.
    _run([args.ty, "check", "--project", cell, "--python", Path(args.engine) / "usr"])
    _bold("buildifier")
    # Do not require repetitive argument and return sections in docstrings.
    _run(
        [
            args.buildifier,
            "-mode=check",
            "-lint=warn",
            "-warnings=-function-docstring-args,-function-docstring-return",
            *_starlark_srcs(args.buck),
        ]
    )
    _bold("starlark lint")
    _run([args.buck, "-v", "0", "starlark", "lint", "--console", "none", cell])
    _bold("starlark typecheck")
    # Typecheck errors use stdout; stderr is only the per-file event log.
    _run([args.buck, "-v", "0", "starlark", "typecheck", cell], stderr=subprocess.DEVNULL)


def _fmt(args: argparse.Namespace) -> None:
    cell = _cell_root(args.buck, "tine")
    _bold("ruff")
    _run([args.ruff, "format", cell])
    _run([args.ruff, "check", "--fix", cell])
    _bold("buildifier")
    _run([args.buildifier, *_starlark_srcs(args.buck)])


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--buck", default="buck", help="buck binary to nest (aliases pass the pinned one; default: PATH)"
    )
    p = argparse.ArgumentParser(prog="dev")
    sub = p.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", parents=[common], help="run the source checks (fmt fixes)")
    for tool in ("buildifier", "ruff", "ty"):
        check.add_argument(f"--{tool}", required=True)
    check.add_argument("--engine", required=True, help="engine root for ty --python")
    check.set_defaults(func=_check)

    fmt = sub.add_parser("fmt", parents=[common], help="auto-format and auto-fix lints")
    for tool in ("buildifier", "ruff"):
        fmt.add_argument(f"--{tool}", required=True)
    fmt.set_defaults(func=_fmt)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
