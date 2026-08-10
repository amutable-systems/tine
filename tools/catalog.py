"""Refresh pure catalog snapshots, then resolve engine transactions against those pins.

Repositories pinned to a mirror that publishes snapshots first advance their declaration to the
newest one the mirror offers. Repositories sharing one pin advance together, and rolling back
means editing the pin.

Remote engine-lock entries retain their package transports, so a repository's package pool keeps
the committed engine available after its repodata advances.

The host orchestrator discovers refresh subtargets and appends only output paths.
Nested Buck reuses the invoking daemon through the inherited isolation directory.
"""

import argparse
import contextlib
import difflib
import itertools
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from util import urlopen, with_retries

VERIFY_PREFIX = ".catalog-verify."

DEFAULT_CATALOG = "tine//catalog"
ENGINE_LABEL = "tine:engine"
REMOTE_REPOSITORY_LABEL = "tine:remote-repository"
RPM_REMOTE_REPOSITORY_LABEL = "tine:rpm-remote-repository"


def _buck_out(buck: str, *args: str) -> str:
    return subprocess.run([buck, *args], check=True, capture_output=True, text=True).stdout.strip()


def _catalog_pattern(catalog: str) -> str:
    package = catalog.removesuffix(":")
    if "//" not in package or ":" in package or "..." in package:
        raise SystemExit(f"catalog: expected a package label, got {catalog!r}")
    return f"{package}:"


def _targets_with_label(buck: str, catalog: str, label: str) -> list[str]:
    """Targets carrying `label` in the selected catalog package."""
    return sorted(_buck_out(buck, "uquery", f"attrfilter(labels, '{label}', {catalog})").split())


def _name_of(target: str) -> str:
    return target.rsplit(":", 1)[1]


def _catalog_directory(buck: str, targets: list[str]) -> Path:
    packages = {target.rsplit(":", 1)[0] for target in targets}
    if len(packages) != 1:
        raise SystemExit(f"catalog: expected targets in one package, found {sorted(packages)}")
    cell, separator, package = packages.pop().partition("//")
    if not separator or not cell:
        raise SystemExit("catalog: Buck returned a target without a canonical cell")
    cell_root = Path(_buck_out(buck, "audit", "cell", cell, "--paths-only"))
    return cell_root / package


def _snapshot_path(target: str, kind: str, suffix: str) -> Path:
    name = _name_of(target)
    if not name.endswith(suffix):
        raise SystemExit(f"catalog: {target} does not end with {suffix!r}")
    return Path("snapshot") / kind / f"{name.removesuffix(suffix)}.json"


def _engine_snapshot_path(target: str) -> Path:
    return _snapshot_path(target, "engine", ".engine")


def _repository_snapshot_path(target: str) -> Path:
    return _snapshot_path(target, "repo", ".repository")


def _run(buck: str, target: str, args: list[str]) -> None:
    # Mute nested Buck while preserving driver progress on stderr.
    subprocess.run([buck, "-v", "0", "run", target, "--console", "none", "--", *args], check=True)


def _pinned_repositories(buck: str, catalog: str, label: str, prefix: str) -> dict[str, dict[str, str]]:
    """Map repository targets carrying a `<prefix>.*` pin to that pin's metadata."""
    out = _buck_out(
        buck,
        "uquery",
        "--json",
        "--output-attribute=^metadata$",
        f"attrfilter(labels, '{label}', {catalog})",
    )
    repositories = {}
    for target, attributes in json.loads(out).items():
        metadata = attributes.get("metadata") or {}
        pin = {
            key.removeprefix(f"{prefix}."): value
            for key, value in metadata.items()
            if key.startswith(f"{prefix}.")
        }
        if "snapshot" in pin and "mirror" in pin:
            repositories[target] = pin
    return repositories


def _rewrite_pin(declaration: Path, attribute: str, current: str, wanted: str) -> None:
    """Repoint every declaration carrying one pin at its successor.

    Every occurrence moves, which is why only a whole pin group is ever advanced: repositories
    written from one literal cannot be moved apart by rewriting it.
    """
    pin = f'{attribute} = "{current}"'
    content = declaration.read_text(encoding="utf-8")
    if pin not in content:
        raise SystemExit(f"catalog: expected {pin!r} in {declaration}")
    declaration.write_text(content.replace(pin, f'{attribute} = "{wanted}"'), encoding="utf-8")


def _advance(
    repositories: dict[str, dict[str, str]],
    selected: set[str],
    newest: Callable[[dict[str, dict[str, str]]], str],
    declaration: Path,
    attribute: str,
) -> None:
    """Advance each distinct pin once, so repositories sharing one stay on one snapshot."""
    groups: dict[str, dict[str, dict[str, str]]] = {}
    for target, pin in sorted(repositories.items()):
        groups.setdefault(pin["snapshot"], {})[target] = pin

    advances = {}
    for current, pins in sorted(groups.items()):
        # Grouping is over the whole catalog, not the selection, because rewriting the literal
        # moves every repository written from it. A group the caller is not about to re-snapshot
        # in full therefore has to stay where it is.
        names = ", ".join(_name_of(target) for target in sorted(pins))
        if not set(pins) <= selected:
            print(f"==> leaving {names} on {current} (outside this refresh)", file=sys.stderr)
            continue
        wanted = newest(pins)
        if wanted != current:
            advances[current] = (wanted, names)

    # Landing on a literal another group holds, or is about to, merges the two: from then on one
    # rewrite moves both and neither can advance alone again. Refuse before rewriting anything, so
    # a deliberately lagging release is never silently collapsed into its sibling and no advance is
    # left half applied.
    for current, (wanted, names) in advances.items():
        others = set(groups) - {current}
        others.update(planned for other, (planned, _) in advances.items() if other != current)
        if wanted in others:
            raise SystemExit(
                f"catalog: {names} would advance onto {wanted!r}, which {attribute} pins "
                "elsewhere; refresh those together"
            )

    for current, (wanted, names) in advances.items():
        print(f"==> advancing {names} to {wanted} (from {current})", file=sys.stderr)
        _rewrite_pin(declaration, attribute, current, wanted)


def _series(snapshot: str) -> str:
    """A snapshot id's series: everything before the trailing datestamp (rpmrepo's naming)."""
    return snapshot.rsplit("-", 1)[0]


def _newest_rpmrepo_snapshot(pins: dict[str, dict[str, str]]) -> str:
    """The newest snapshot every repository sharing one rpmrepo pin can move to."""
    wanted = {
        _newest_snapshot(_name_of(target), pin["mirror"], _series(pin["snapshot"]))
        for target, pin in pins.items()
    }
    if len(wanted) != 1:
        raise SystemExit(f"catalog: {sorted(pins)} disagree on their successor: {sorted(wanted)}")
    return wanted.pop()


def _newest_snapshot(repository: str, mirror: str, series: str) -> str:
    """The newest snapshot of `series` that the mirror's gateway enumerates."""
    gateway, found, _ = mirror.partition("/v2/mirror/")
    if not found:
        raise SystemExit(f"{repository}: mirror {mirror!r} is not an rpmrepo /v2/mirror/ URL")

    def enumerate_snapshots() -> list[object]:
        with urlopen(gateway + "/v2/enumerate", agent="tine-catalog") as response:
            return json.load(response)

    snapshots = with_retries(f"{repository}: enumerate", enumerate_snapshots)
    matches = [s for s in snapshots if isinstance(s, str) and _series(s) == series]
    if not matches:
        raise SystemExit(f"{repository}: the mirror enumerates no {series!r} snapshots")
    # Snapshot ids end in a datestamp, so the newest sorts last.
    return max(matches)


def _advance_snapshots(buck: str, catalog: str, catalog_dir: Path, selected: list[str]) -> None:
    """Advance the selected mirror-pinned repositories to the newest snapshot their mirror offers.

    Advancing a pin without re-snapshotting the repository it belongs to would leave a base URL
    from one snapshot composing package locations from another, so this stays inside the selection the
    caller is about to snapshot.
    """
    declaration = catalog_dir / "BUCK"
    wanted = set(selected)
    for label, prefix, attribute, newest in (
        (RPM_REMOTE_REPOSITORY_LABEL, "rpmrepo", "rpmrepo_snapshot", _newest_rpmrepo_snapshot),
    ):
        pinned = _pinned_repositories(buck, catalog, label, prefix)
        _advance(pinned, wanted, newest, declaration, attribute)


def _snapshot(buck: str, target: str, out: Path) -> None:
    """Atomically replace a repository snapshot with its current pure metadata."""
    print(f"==> snapshotting {_name_of(target)} (via {target}[snapshot])", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(buck, f"{target}[snapshot]", ["--out", str(out)])


def _resolve(buck: str, target: str, out: Path) -> None:
    print(f"==> resolving {_name_of(target)} (via {target}[resolve])", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(buck, f"{target}[resolve]", ["--out", str(out)])


def _select_engines(all_resolves: list[str], selected_engines: list[str] | None) -> list[str]:
    if selected_engines is None:
        return all_resolves
    duplicates = sorted({name for name in selected_engines if selected_engines.count(name) > 1})
    if duplicates:
        raise SystemExit(f"catalog: engine names selected more than once: {duplicates}")
    by_name = {_name_of(target): target for target in all_resolves}
    unknown = sorted(set(selected_engines) - by_name.keys())
    if unknown:
        raise SystemExit(f"catalog: unknown engine names: {unknown}")
    selected = set(selected_engines)
    return [target for target in all_resolves if _name_of(target) in selected]


def _repositories_for_engines(buck: str, engines: list[str]) -> list[str]:
    """Remote repository targets reachable from the given engine targets."""
    engine_set = " ".join(engines)
    query = f"attrfilter(labels, '{REMOTE_REPOSITORY_LABEL}', deps(set({engine_set})))"
    return sorted(_buck_out(buck, "uquery", query).split())


def _refresh(
    buck: str,
    catalog: str,
    selected_engines: list[str] | None,
    advance_snapshots: bool,
    destination: Path | None = None,
) -> tuple[Path, list[Path]]:
    """Snapshot repositories and resolve selected engines into `destination`, the catalog by default.

    Returns the catalog directory and what was written, relative to whichever it was written to.

    Selecting engines also scopes the snapshotted repositories to those the engines depend on, so a
    partial refresh or verify never touches a repository outside the selection.
    """
    all_resolves = _targets_with_label(buck, catalog, ENGINE_LABEL)
    resolves = _select_engines(all_resolves, selected_engines)

    if selected_engines is None:
        snapshots = _targets_with_label(buck, catalog, REMOTE_REPOSITORY_LABEL)
    else:
        snapshots = _repositories_for_engines(buck, resolves)
    targets = all_resolves + snapshots
    if not targets:
        raise SystemExit(f"catalog: no repository/engine refresh targets found in {catalog}")
    catalog_dir = _catalog_directory(buck, targets)
    out_dir = destination if destination is not None else catalog_dir
    if advance_snapshots:
        _advance_snapshots(buck, catalog, catalog_dir, snapshots)

    written = []
    for target in snapshots:
        written.append(_repository_snapshot_path(target))
        _snapshot(buck, target, out_dir / written[-1])

    for target in resolves:
        written.append(_engine_snapshot_path(target))
        _resolve(buck, target, out_dir / written[-1])

    return catalog_dir, written


def _differences(committed: Path, regenerated: Path, limit: int = 24) -> str:
    """What a regenerated file says that the committed one does not, bounded.

    A snapshot runs to tens of thousands of lines, so a full diff of one is unreadable and a diff
    of five is worse; enough to name what moved is the useful amount.
    """
    if not committed.exists():
        return f"{committed}: not committed yet\n"
    expected = committed.read_text(encoding="utf-8").splitlines(keepends=True)
    actual = regenerated.read_text(encoding="utf-8").splitlines(keepends=True)
    if expected == actual:
        return ""
    diff = difflib.unified_diff(expected, actual, fromfile=str(committed), tofile="regenerated", n=0)
    shown = list(itertools.islice(diff, limit))
    if len(shown) == limit:
        shown.append("... (truncated)\n")
    return "".join(shown)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="catalog")
    p.add_argument(
        "catalog",
        nargs="?",
        default=DEFAULT_CATALOG,
        help=f"catalog package to refresh (default: {DEFAULT_CATALOG})",
    )
    p.add_argument(
        "--buck", default="buck", help="buck binary to nest (aliases pass the pinned one; default: PATH)"
    )
    p.add_argument(
        "--engine",
        action="append",
        help="only (re)resolve these engines and snapshot the repositories they depend on; default: all",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="assert the committed catalog matches what the pinned resolvers produce (CI)",
    )
    args = p.parse_args(argv)
    catalog = _catalog_pattern(args.catalog)

    # Run nested commands from the project root so wrappers resolve consistently.
    with contextlib.chdir(_buck_out(args.buck, "root", "--kind", "project")) as _:
        if not args.verify:
            _refresh(args.buck, catalog, args.engine, advance_snapshots=True)
            return

        # Regenerate beside the catalog rather than over it, so a verify that fails, or dies,
        # leaves the checkout exactly as it found it. It has to stay inside the project: the
        # `[resolve]` sub-target runs in a sandbox that binds the project and nothing else, so an
        # output anywhere else would land in the sandbox's own tmpfs.
        with tempfile.TemporaryDirectory(dir=".", prefix=VERIFY_PREFIX) as scratch:
            catalog_dir, written = _refresh(
                args.buck,
                catalog,
                args.engine,
                advance_snapshots=False,
                destination=Path(scratch),
            )
            print("==> verifying the committed catalog matches", file=sys.stderr)
            stale = [
                report
                for relative in written
                if (report := _differences(catalog_dir / relative, Path(scratch) / relative))
            ]

    if stale:
        for report in stale:
            print(report, end="", file=sys.stderr)
        raise SystemExit("catalog: the committed catalog is not what the pinned resolvers produce")


if __name__ == "__main__":
    main()
