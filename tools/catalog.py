"""Refresh pure catalog snapshots, then resolve box transactions against those pins.

Repositories pinned to a mirror that publishes snapshots first advance their declaration to the
newest one: an rpmrepo gateway enumerates them, while the Arch Linux Archive publishes a tree
per day. Repositories sharing one pin advance together, and rolling back means editing the pin.

Remote box-lock entries retain their package transports, so a repository's package pool keeps
the committed box available after its repodata advances.

The host orchestrator discovers refresh subtargets and takes each result from the driver's stdout,
so refreshing and verifying run one command and only the host decides where an output belongs.
Nested Buck reuses the invoking daemon through the inherited isolation directory.
"""

import argparse
import contextlib
import difflib
import itertools
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from util import atomic_write_text, urlopen, with_retries

DEFAULT_CATALOG = "tine//catalog"
BOX_LABEL = "tine:box"
REMOTE_REPOSITORY_LABEL = "tine:remote-repository"
RPM_REMOTE_REPOSITORY_LABEL = "tine:rpm-remote-repository"
PACMAN_REMOTE_REPOSITORY_LABEL = "tine:pacman-remote-repository"


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


def _box_snapshot_path(target: str) -> Path:
    return _snapshot_path(target, "box", ".box")


def _repository_snapshot_path(target: str) -> Path:
    return _snapshot_path(target, "repo", ".repository")


def _run(buck: str, target: str) -> str:
    """Run a refresh subtarget, returning what it wrote to stdout.

    Buck execs the target rather than piping it, and its own output is on stderr, so stdout is
    the driver's alone. That is the only channel out: the hermetic sandbox a driver runs in binds
    the project and nothing else, so a path outside it would land in the sandbox's own tmpfs.
    """
    # Mute nested Buck while preserving driver progress on stderr.
    command = [buck, "-v", "0", "run", target, "--console", "none", "--", "--out", "-"]
    return subprocess.run(command, check=True, stdout=subprocess.PIPE, encoding="utf-8").stdout


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


def _newest_archive_snapshot(pins: dict[str, dict[str, str]]) -> str:
    """The newest day the archive has finished publishing.

    The archive publishes a tree per day rather than an index to enumerate, but it does record when
    it last finished one, which is the only thing that says a day is complete rather than half
    written. Every repository in a group shares the pin, so one marker answers for all of them.
    """
    pin = next(iter(pins.values()))
    # Keep in sync with pacman_remote_repository's base URL, whose mirror this is rooted at.
    url = pin["mirror"].rstrip("/") + "/last/lastsync"

    def lastsync() -> str:
        with urlopen(url, agent="tine-catalog") as response:
            synced = datetime.fromtimestamp(int(response.read().strip()), UTC)
        return synced.strftime("%Y/%m/%d")

    # An advance never moves a pin backwards.
    return max(with_retries("archive: lastsync", lastsync), pin["snapshot"])


def _advance_snapshots(buck: str, catalog: str, catalog_dir: Path, selected: list[str]) -> None:
    """Advance the selected mirror-pinned repositories to the newest snapshot their mirror offers.

    Advancing a pin without re-snapshotting the repository it belongs to would leave a base URL
    from one day composing package locations from another, so this stays inside the selection the
    caller is about to snapshot.
    """
    declaration = catalog_dir / "BUCK"
    wanted = set(selected)
    for label, prefix, attribute, newest in (
        (RPM_REMOTE_REPOSITORY_LABEL, "rpmrepo", "rpmrepo_snapshot", _newest_rpmrepo_snapshot),
        (PACMAN_REMOTE_REPOSITORY_LABEL, "archlinux", "archive_snapshot", _newest_archive_snapshot),
    ):
        pinned = _pinned_repositories(buck, catalog, label, prefix)
        _advance(pinned, wanted, newest, declaration, attribute)


def _snapshot(buck: str, target: str) -> str:
    """One repository's current pure metadata."""
    print(f"==> snapshotting {_name_of(target)} (via {target}[snapshot])", file=sys.stderr)
    return _run(buck, f"{target}[snapshot]")


def _resolve(buck: str, target: str) -> str:
    print(f"==> resolving {_name_of(target)} (via {target}[resolve])", file=sys.stderr)
    return _run(buck, f"{target}[resolve]")


def _select_boxes(all_resolves: list[str], selected_boxes: list[str] | None) -> list[str]:
    if selected_boxes is None:
        return all_resolves
    duplicates = sorted({name for name in selected_boxes if selected_boxes.count(name) > 1})
    if duplicates:
        raise SystemExit(f"catalog: box names selected more than once: {duplicates}")
    by_name = {_name_of(target): target for target in all_resolves}
    unknown = sorted(set(selected_boxes) - by_name.keys())
    if unknown:
        raise SystemExit(f"catalog: unknown box names: {unknown}")
    selected = set(selected_boxes)
    return [target for target in all_resolves if _name_of(target) in selected]


def _repositories_for_boxes(buck: str, boxes: list[str]) -> list[str]:
    """Remote repository targets reachable from the given box targets."""
    box_set = " ".join(boxes)
    query = f"attrfilter(labels, '{REMOTE_REPOSITORY_LABEL}', deps(set({box_set})))"
    return sorted(_buck_out(buck, "uquery", query).split())


def _refresh(
    buck: str,
    catalog: str,
    selected_boxes: list[str] | None,
    advance_snapshots: bool,
) -> Iterator[tuple[Path, str]]:
    """Snapshot repositories and resolve selected boxes, yielding each result and where it belongs.

    Nothing is written here, so a verify regenerates through the same commands a refresh does and
    still leaves the checkout exactly as it found it.

    Selecting boxes also scopes the snapshotted repositories to those the boxes depend on, so a
    partial refresh or verify never touches a repository outside the selection.
    """
    all_resolves = _targets_with_label(buck, catalog, BOX_LABEL)
    resolves = _select_boxes(all_resolves, selected_boxes)

    if selected_boxes is None:
        snapshots = _targets_with_label(buck, catalog, REMOTE_REPOSITORY_LABEL)
    else:
        snapshots = _repositories_for_boxes(buck, resolves)
    targets = all_resolves + snapshots
    if not targets:
        raise SystemExit(f"catalog: no repository/box refresh targets found in {catalog}")
    catalog_dir = _catalog_directory(buck, targets)
    if advance_snapshots:
        _advance_snapshots(buck, catalog, catalog_dir, snapshots)

    for target in snapshots:
        yield catalog_dir / _repository_snapshot_path(target), _snapshot(buck, target)

    for target in resolves:
        yield catalog_dir / _box_snapshot_path(target), _resolve(buck, target)


def _differences(committed: Path, regenerated: str, limit: int = 24) -> str:
    """What a regenerated file says that the committed one does not, bounded.

    A snapshot runs to tens of thousands of lines, so a full diff of one is unreadable and a diff
    of five is worse; enough to name what moved is the useful amount.
    """
    if not committed.exists():
        return f"{committed}: not committed yet\n"
    expected = committed.read_text(encoding="utf-8").splitlines(keepends=True)
    actual = regenerated.splitlines(keepends=True)
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
        "--buck",
        # `tine` exports the Buck2 it resolved, so a nested command runs that one and not the
        # wrapper: refreshing configuration under a command already holding it deadlocks.
        default=os.environ.get("BUCK2_BINARY", "buck"),
        help="buck binary to nest (default: $BUCK2_BINARY, else PATH)",
    )
    p.add_argument(
        "--box",
        action="append",
        help="only (re)resolve these boxes and snapshot the repositories they depend on; default: all",
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
        regenerated = _refresh(args.buck, catalog, args.box, advance_snapshots=not args.verify)
        if not args.verify:
            for path, content in regenerated:
                atomic_write_text(path, content)
            return

        print("==> verifying the committed catalog matches", file=sys.stderr)
        stale = [report for path, content in regenerated if (report := _differences(path, content))]

    if stale:
        for report in stale:
            print(report, end="", file=sys.stderr)
        raise SystemExit("catalog: the committed catalog is not what the pinned resolvers produce")


if __name__ == "__main__":
    main()
