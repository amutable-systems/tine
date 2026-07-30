"""Run pinned source checks and formatters against the active tine cell."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _bold(label: str) -> None:
    print(f"\033[1m{label}\033[0m", flush=True)


def _run(cmd: list[str | Path], *, stderr: int | None = None) -> None:
    # Tools print their own diagnostics; propagate failures without a traceback.
    proc = subprocess.run(cmd, stderr=stderr)
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
    aliases = json.loads(_buck_out(buck, "audit", "cell", "--json", "--aliases"))
    roots = {path: name for name, path in sorted(cells.items()) if name not in ("none", "prelude")}
    universe = " + ".join(sorted(f"{name}//..." for name in roots.values()))
    project = Path(_buck_out(buck, "root", "--kind", "project"))
    files = sorted(
        project / f for f in _buck_out(buck, "uquery", f"allbuildfiles({universe})").splitlines() if f
    )
    # Attribute each file to the innermost cell holding it. Standalone, tine is the root cell, so a
    # plain prefix test would also claim every nested cell's files, including the bundled prelude's
    # (which are not on disk, and which buildifier then skips without failing).
    cell_roots = sorted((Path(p) for p in cells.values()), key=lambda p: len(p.parts), reverse=True)
    tine = Path(aliases["tine"])

    def owner(path: Path) -> Path | None:
        return next((root for root in cell_roots if path.is_relative_to(root)), None)

    return [f for f in files if owner(f) == tine and f.suffix != ".json"]


def _lint(args: argparse.Namespace) -> None:
    cell = _cell_root(args.buck, "tine")
    srcs = _starlark_srcs(args.buck)
    _bold("ruff")
    _run([args.ruff, "format", "--check", "--no-cache", cell])
    _run([args.ruff, "check", "--no-cache", cell])
    _bold("ty")
    # Resolve third-party imports from the pinned engine runtime.
    _run(
        [
            args.ty,
            "check",
            "--project",
            cell,
            "--python",
            Path(args.engine) / "usr",
        ]
    )
    _bold("buildifier")
    # Do not require repetitive argument and return sections in docstrings.
    _run(
        [
            args.buildifier,
            "-mode=check",
            "-lint=warn",
            "-warnings=-function-docstring-args,-function-docstring-return",
            *srcs,
        ]
    )
    # Pass the files rather than the cell directory: standalone, the tine cell is the project root,
    # which Buck normalizes to an empty path and rejects.
    _bold("starlark lint")
    _run([args.buck, "-v", "0", "starlark", "lint", "--console", "none", *srcs])
    _bold("starlark typecheck")
    # Typecheck errors use stdout; stderr is only the per-file event log.
    _run([args.buck, "-v", "0", "starlark", "typecheck", *srcs], stderr=subprocess.DEVNULL)


def _fmt(args: argparse.Namespace) -> None:
    cell = _cell_root(args.buck, "tine")
    _bold("ruff")
    _run([args.ruff, "format", cell])
    _run([args.ruff, "check", "--fix", cell])
    _bold("buildifier")
    _run([args.buildifier, *_starlark_srcs(args.buck)])


def _write_dot(path: Path, intra: dict[str, list[str]], rev: dict[str, list[str]]) -> None:
    """The cycle subgraph as graphviz; node labels carry out/in degree within the cycle."""
    lines = ["digraph scc {", "  rankdir=LR;", "  node [shape=box, fontsize=10];"]
    lines += [f'  "{n}" [label="{n}\\n->{len(intra[n])} <-{len(rev[n])}"];' for n in sorted(intra)]
    lines += [f'  "{n}" -> "{p}";' for n in sorted(intra) for p in intra[n]]
    path.write_text("\n".join(lines) + "\n}\n")


def _scc(args: argparse.Namespace) -> None:
    # Graph derivation and cycle detection live in buck (rpm_branch); this only formats its output.
    data = json.loads(_buck_out(args.buck, "build", f"{args.branch}:_buildrequires_graph", "--out", "-"))
    edge_caps: dict[str, dict[str, list[str]]] = data["edges"]
    components: dict[int, set[str]] = {}
    for name, cid in data["sccs"].items():
        components.setdefault(cid, set()).add(name)
    cycles = sorted((c for c in components.values() if len(c) > 1), key=len, reverse=True)

    print(f"{len(edge_caps)} packages; cycle sizes: {[len(c) for c in cycles] or 'none, all acyclic'}")
    acyclic = sorted(n for c in components.values() if len(c) == 1 for n in c)
    print(f"outside any cycle: {', '.join(acyclic)}\n")
    if not cycles:
        return

    incycle = {n: c for c in cycles for n in c}  # cycle member -> its cycle
    intra = {n: sorted(p for p in edge_caps[n] if incycle.get(p) is incycle[n]) for n in incycle}
    rev: dict[str, list[str]] = {n: [] for n in incycle}
    for n in sorted(intra):
        for p in intra[n]:
            rev[p].append(n)

    if args.why:
        assert args.why in incycle, f"{args.why} is not a member of any cycle"
        _bold(f"{args.why}: cycle edges and their reasons")
        for p in intra[args.why]:
            print(f"  {args.why} -> {p}: {', '.join(edge_caps[args.why][p])}")
        for n in rev[args.why]:
            print(f"  {n} -> {args.why}: {', '.join(edge_caps[n][args.why])}")
        return

    for cycle in cycles:
        _bold(f"the {len(cycle)}-member cycle, ascending peer-provider count")
        print(f"{'package':24} {'out':>3} {'in':>3}  cycle-internal BR providers")
        for n in sorted(cycle, key=lambda n: (len(intra[n]), n)):
            print(f"{n:24} {len(intra[n]):3} {len(rev[n]):3}  {', '.join(intra[n])}")
        print()

    if args.dot:
        _write_dot(args.dot, intra, rev)
        print(f"\nwrote {args.dot}")


def _ty(args: argparse.Namespace) -> None:
    engine = Path(args.engine).resolve()
    site_packages = sorted((engine / "usr/lib").glob("python*/site-packages"))
    if len(site_packages) != 1:
        raise SystemExit(f"ty: expected one site-packages directory in {engine}, found {len(site_packages)}")
    env = os.environ.copy()
    pythonpath = [str(site_packages[0])]
    if inherited := env.get("PYTHONPATH"):
        pythonpath.append(inherited)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    executable = Path(args.ty).resolve()
    os.execve(executable, [str(executable), *args.arguments], env)


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--buck", default="buck", help="buck binary to nest (aliases pass the pinned one; default: PATH)"
    )
    p = argparse.ArgumentParser(prog="dev")
    sub = p.add_subparsers(dest="command", required=True)

    lint = sub.add_parser("lint", parents=[common], help="run the source lints (fmt fixes)")
    for tool in ("buildifier", "ruff", "ty"):
        lint.add_argument(f"--{tool}", required=True)
    lint.add_argument("--engine", required=True, help="engine root for ty --python")
    lint.set_defaults(func=_lint)

    fmt = sub.add_parser("fmt", parents=[common], help="auto-format and auto-fix lints")
    for tool in ("buildifier", "ruff"):
        fmt.add_argument(f"--{tool}", required=True)
    fmt.set_defaults(func=_fmt)

    scc = sub.add_parser("scc", parents=[common], help="analyze a branch's BuildRequires cycles")
    scc.add_argument("branch", help="branch label, e.g. //packages/fedora/rawhide")
    scc.add_argument("--why", metavar="PKG", help="show one cycle member's edges and their reasons")
    scc.add_argument("--dot", type=Path, help="write the cycle subgraph as graphviz")
    scc.set_defaults(func=_scc)

    ty = sub.add_parser("ty", help="run pinned ty against the engine's Python environment")
    ty.add_argument("--engine", required=True)
    ty.add_argument("--ty", required=True)
    ty.add_argument("arguments", nargs=argparse.REMAINDER)
    ty.set_defaults(func=_ty)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
