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
    # Keep the tine cell's own files, plus those of any cell nested inside it. Reject by owning cell
    # rather than by path prefix, because nested `none`/`prelude` are not on disk, so a prefix test
    # would hand the formatter paths that do not exist.
    cell_roots = sorted(
        ((Path(path), name) for name, path in cells.items()),
        key=lambda item: len(item[0].parts),
        reverse=True,
    )
    tine = Path(aliases["tine"])

    def owner(path: Path) -> str | None:
        return next((name for root, name in cell_roots if path.is_relative_to(root)), None)

    return [
        f
        for f in files
        # Data loads are parse inputs too, but not Starlark, and the format override means their
        # names promise nothing, so keep what is known to be Starlark rather than reject data.
        if f.is_relative_to(tine) and owner(f) not in (None, "none", "prelude")
        if f.suffix == ".bzl" or f.name in ("BUCK", "PACKAGE")
    ] + sorted(
        # A .bxl is Starlark nothing loads, so no build file names it; check it all the same.
        f
        for f in tine.rglob("*.bxl")
        if "buck-out" not in f.parts
    )


def _orphan_tests(buck: str, cell: Path) -> list[Path]:
    """Test files no box_python_test lists in `srcs`, which `buck test` would never run.

    Each suite names its sources explicitly, so a new file beside the code is invisible until it is
    added to one; nothing else would report that.
    """
    targets = json.loads(
        _buck_out(
            buck, "-v", "0", "uquery", "kind('box_python_test', tine//...)", "--output-attribute", "srcs"
        )
    )
    claimed = {cell / src.split("//", 1)[1] for target in targets.values() for src in target["srcs"]}
    return sorted(p for p in cell.rglob("*_test.py") if "buck-out" not in p.parts and p not in claimed)


def _starlark_fmt(args: argparse.Namespace, *arguments: str | Path) -> list[str | Path]:
    return [args.starlark_fmt, "--config", args.starlark_fmt_config, *arguments]


def _fmt_diff(args: argparse.Namespace, src: Path) -> str:
    """The rewrite starlark_fmt would apply to one file, empty when it is already formatted."""
    proc = subprocess.run(_starlark_fmt(args, "diff", src), capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr, end="", file=sys.stderr, flush=True)
        raise SystemExit(proc.returncode)
    return proc.stdout


def _lint(args: argparse.Namespace) -> None:
    cell = _cell_root(args.buck, "tine")
    srcs = _starlark_srcs(args.buck)
    _bold("test targets")
    if orphans := _orphan_tests(args.buck, cell):
        listing = "\n".join(f"  {p.relative_to(cell)}" for p in orphans)
        raise SystemExit(f"no box_python_test lists these, so they never run:\n{listing}")
    _bold("ruff")
    _run([args.ruff, "format", "--check", "--no-cache", cell])
    _run([args.ruff, "check", "--no-cache", cell])
    _bold("ty")
    targets = _buck_out(args.buck, "uquery", "attrfilter(labels, 'python-typecheck', tine//...)").split()
    if not targets:
        raise SystemExit("ty: no generated type-check targets found")
    _run([args.buck, "build", *targets])
    _bold("starlark_fmt")
    # starlark_fmt has no check mode, so diff each file and fail on the first rewrite it would make.
    if diffs := [diff for src in srcs if (diff := _fmt_diff(args, src))]:
        print("".join(diffs), end="")
        raise SystemExit(1)
    # Pass the files rather than the cell directory: standalone, the tine cell is the project root,
    # which Buck normalizes to an empty path and rejects.
    _bold("starlark lint")
    _run([args.buck, "-v", "0", "starlark", "lint", "--console", "none", *srcs])
    _bold("starlark typecheck")
    # Typecheck errors use stdout; stderr is only the per-file event log. Unlike lint, typecheck
    # follows load() into data files and parses them as Starlark, which no TOML survives (a JSON
    # object happens to be a valid Starlark expression), so files with a TOML load stay out; the
    # probe also matches the `?format=toml` spelling.
    checkable = [f for f in srcs if 'toml"' not in f.read_text(encoding="utf-8")]
    _run([args.buck, "-v", "0", "starlark", "typecheck", *checkable], stderr=subprocess.DEVNULL)
    _bold("target graph")
    # Analysis, not a build: it reaches every rule a build would run, without producing anything.
    # Scoped to this cell, whose platform the parser knows how to detect; what a consuming project
    # declares is its own to check, with its own pattern.
    _run([args.buck, "-v", "0", "bxl", "--console", "none", "tine//tools/graph.bxl:analyze"])


def _check(args: argparse.Namespace) -> None:
    _lint(args)
    _bold("unit tests")
    # Building an example image is minutes where these are seconds. Everything that needs one is
    # labelled `image` and covered by `buck test tine//... --include image`, which is what CI runs.
    _run([args.buck, "test", "tine//...", "--exclude", "image"])


def _fmt(args: argparse.Namespace) -> None:
    # Ask Buck everything before the formatters touch the tree. This runs under `buck run`, whose
    # command stays active for as long as the binary does, and buck2 only recognizes a nested
    # command as nested when it spawned the process itself, which it does for actions but not for
    # run targets. A query issued after a write therefore needs a newer state than the command it
    # is nested in and waits for it to finish: a deadlock rather than an error.
    cell = _cell_root(args.buck, "tine")
    srcs = _starlark_srcs(args.buck)
    _bold("ruff")
    _run([args.ruff, "format", "--no-cache", cell])
    _run([args.ruff, "check", "--fix", "--no-cache", cell])
    _bold("starlark_fmt")
    _run(_starlark_fmt(args, "fmt", *srcs))


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
    box = Path(args.box)
    site_packages = sorted((box / "usr/lib").glob("python*/site-packages"))
    if len(site_packages) != 1:
        raise SystemExit(f"ty: expected one site-packages directory in {box}, found {len(site_packages)}")
    env = os.environ.copy()
    pythonpath = [str(site_packages[0])]
    if inherited := env.get("PYTHONPATH"):
        pythonpath.append(inherited)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    executable = Path(args.ty).absolute()
    os.execve(executable, [str(executable), *args.arguments], env)


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--buck",
        # `tine` exports the Buck2 it resolved, so a nested command runs that one and not the
        # wrapper: refreshing configuration under a command already holding it deadlocks.
        default=os.environ.get("BUCK2_BINARY", "buck"),
        help="buck binary to nest (default: $BUCK2_BINARY, else PATH)",
    )
    starlark = argparse.ArgumentParser(add_help=False)
    starlark.add_argument("--starlark-fmt", required=True)
    starlark.add_argument("--starlark-fmt-config", required=True, help="starlark_fmt --config tables")
    p = argparse.ArgumentParser(prog="dev")
    sub = p.add_subparsers(dest="command", required=True)

    for name, func, help_text in (
        ("lint", _lint, "run the source lints (fmt fixes)"),
        ("check", _check, "run the source lints, then every unit-test suite"),
    ):
        verb = sub.add_parser(name, parents=[common, starlark], help=help_text)
        verb.add_argument("--ruff", required=True)
        verb.set_defaults(func=func)

    fmt = sub.add_parser("fmt", parents=[common, starlark], help="auto-format and auto-fix lints")
    fmt.add_argument("--ruff", required=True)
    fmt.set_defaults(func=_fmt)

    scc = sub.add_parser("scc", parents=[common], help="analyze a branch's BuildRequires cycles")
    scc.add_argument("branch", help="branch label, e.g. //packages/fedora/rawhide")
    scc.add_argument("--why", metavar="PKG", help="show one cycle member's edges and their reasons")
    scc.add_argument("--dot", type=Path, help="write the cycle subgraph as graphviz")
    scc.set_defaults(func=_scc)

    ty = sub.add_parser("ty", help="run pinned ty against the box's Python environment")
    ty.add_argument("--box", required=True)
    ty.add_argument("--ty", required=True)
    ty.add_argument("arguments", nargs=argparse.REMAINDER)
    ty.set_defaults(func=_ty)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
