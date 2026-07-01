"""dev — the cell's dev commands: check (every source check) and fmt (the fixers).

Runs on the host (a python_bootstrap_binary), invoked via the command_alias targets here:
`buck run tine//tools:<check|fmt>`. The driver's job is what a command_alias can't bake
in statically: paths resolved at runtime (`buck audit cell`, `buck uquery`) and host-side
sequencing. The tools run against the working tree and stream straight to the terminal
(buck actions can't relay tool output live — facebook/buck2#760). Nested buck reuses the
daemon (see buckify.py for why that holds and why the buck binary is passed in).
"""

import argparse
import json
import subprocess
from pathlib import Path


def _bold(label: str) -> None:
    print(f"\033[1m{label}\033[0m", flush=True)


def _run(cmd: list[str | Path], **kwargs) -> None:
    # The tools print their own diagnostics; on failure just propagate the exit code
    # (fail-fast) without a traceback.
    proc = subprocess.run(cmd, **kwargs)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def _buck_out(buck: str, *args: str) -> str:
    return subprocess.run([buck, *args], check=True, capture_output=True, text=True).stdout.strip()


def _cell_root(buck: str, cell: str) -> Path:
    return Path(_buck_out(buck, "audit", "cell", cell, "--paths-only"))


def _starlark_srcs(buck: str) -> list[Path]:
    # The graph-derived starlark surface (aspect-style — rules_lint's model): every BUCK
    # file plus every .bzl anything loads, so files loaded only by a consumer cell still
    # belong to the check and orphan files are ignored as dead code. The universe is
    # every in-repo cell; the prelude (external, bundled) and the deliberately empty
    # `none` cell aren't ours to query. The result is kept to the tine tree's own files
    # (tine itself plus the embedded toolchains/catalog cells). (No `-v 0` on uquery:
    # it mutes results.)
    cells = json.loads(_buck_out(buck, "audit", "cell", "--json"))
    roots = {path: name for name, path in sorted(cells.items()) if name not in ("none", "prelude")}
    universe = " + ".join(sorted(f"{name}//..." for name in roots.values()))
    project = Path(_buck_out(buck, "root", "--kind", "project"))
    files = sorted(
        project / f for f in _buck_out(buck, "uquery", f"allbuildfiles({universe})").splitlines() if f
    )
    return [f for f in files if f.is_relative_to(Path(cells["tine"]))]


def _check(args: argparse.Namespace) -> None:
    # fmt's surface in check mode (so check and fmt always agree), plus ty and buck's
    # starlark lint/typecheck subcommands. The tools run directly against the working
    # tree and stream their own (tty-colored) diagnostics; the first failing tool ends
    # the run. Everything but buildifier walks the cell root itself (buck-out and
    # vendored code are excluded in pyproject.toml).
    cell = _cell_root(args.buck, "tine")
    _bold("ruff")
    _run([args.ruff, "format", "--check", "--no-cache", cell])
    _run([args.ruff, "check", "--no-cache", cell])
    _bold("ty")
    # ty resolves config from a project dir, not (like ruff) from the checked files'
    # ancestry, and checks the whole project when given no paths. Third-party imports
    # (libdnf5, createrepo_c) resolve from the engine root — the pinned environment the
    # drivers actually run in — which buck builds up front via the alias's `$(location …)`.
    _run([args.ty, "check", "--project", cell, "--python", Path(args.engine) / "usr"])
    _bold("buildifier")
    # -lint=warn runs buildifier's linter (default set); the two `function-docstring`
    # subtractions drop the mandate to document every arg + return value (too verbose for
    # our prose docstrings).
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
    # typecheck's stderr is a per-file event log — dropped; errors go to stdout.
    _run([args.buck, "-v", "0", "starlark", "typecheck", cell], stderr=subprocess.DEVNULL)


def _fmt(args: argparse.Namespace) -> None:
    # The same surface as check: ruff walks the cell root, buildifier takes the
    # graph-derived starlark set.
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
