# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Refresh pure catalog snapshots, then resolve box transactions against those pins.

Repositories pinned to a mirror that publishes snapshots first advance their declaration to the
newest one: an rpmrepo gateway enumerates them, while the Arch Linux Archive publishes a tree
per day. Repositories sharing one pin advance together, and rolling back means editing the pin.

Remote box-lock entries retain their package transports, so a repository's package pool keeps
the committed box available after its repodata advances.

A repository's declared signing keys are fetched once, by fingerprint, and the file is never
rewritten afterwards. Whether a file is the declared key is the build's check, with the package
system's own tools.

The host orchestrator discovers refresh subtargets and takes each result from the driver's stdout,
so refreshing and verifying run one command and only the host decides where an output belongs.
Nested Buck reuses the invoking daemon through the inherited isolation directory.
"""

import argparse
import contextlib
import difflib
import itertools
import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from util import (
    atomic_write_text,
    buck_output,
    commit_paths,
    fail,
    nested_buck,
    package_directory,
    urlopen,
    with_retries,
)

DEFAULT_CATALOG = "tine//catalog"
BOX_LABEL = "tine:box"
REMOTE_REPOSITORY_LABEL = "tine:remote-repository"
RPM_REMOTE_REPOSITORY_LABEL = "tine:rpm-remote-repository"
PACMAN_REMOTE_REPOSITORY_LABEL = "tine:pacman-remote-repository"

# Keep in sync with SIGNING_KEY_DIRECTORY/SIGNING_KEY_SUFFIX in package/repository.bzl.
SIGNING_KEY_DIRECTORY = Path("snapshot/key")
SIGNING_KEY_SUFFIX = ".key"
ARMOR_HEADER = b"-----BEGIN PGP PUBLIC KEY BLOCK-----"


def _catalog_pattern(catalog: str) -> str:
    package = catalog.removesuffix(":")
    if "//" not in package or ":" in package or "..." in package:
        fail(f"catalog: expected a package label, got {catalog!r}")
    return f"{package}:"


def _targets_with_label(buck: str, catalog: str, label: str) -> list[str]:
    """Targets carrying `label` in the selected catalog package."""
    return sorted(buck_output(buck, "uquery", f"attrfilter(labels, '{label}', {catalog})").split())


def _name_of(target: str) -> str:
    return target.rsplit(":", 1)[1]


def _catalog_directory(buck: str, targets: list[str]) -> Path:
    packages = {target.rsplit(":", 1)[0] for target in targets}
    if len(packages) != 1:
        fail(f"catalog: expected targets in one package, found {sorted(packages)}")
    return package_directory(buck, packages.pop())


def _snapshot_path(target: str, kind: str, suffix: str) -> Path:
    name = _name_of(target)
    if not name.endswith(suffix):
        fail(f"catalog: {target} does not end with {suffix!r}")
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
    out = buck_output(
        buck,
        "uquery",
        "--json",
        "--output-attribute=^metadata$",
        f"attrfilter(labels, '{label}', {catalog})",
    )
    repositories: dict[str, dict[str, str]] = {}
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
        fail(f"catalog: expected {pin!r} in {declaration}")
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
            fail(
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
        fail(f"catalog: {sorted(pins)} disagree on their successor: {sorted(wanted)}")
    return wanted.pop()


def _newest_snapshot(repository: str, mirror: str, series: str) -> str:
    """The newest snapshot of `series` that the mirror's gateway enumerates."""
    gateway, found, _ = mirror.partition("/v2/mirror/")
    if not found:
        fail(f"{repository}: mirror {mirror!r} is not an rpmrepo /v2/mirror/ URL")

    def enumerate_snapshots() -> list[object]:
        with urlopen(gateway + "/v2/enumerate", agent="tine-catalog") as response:
            return cast(list[object], json.load(response))

    snapshots = with_retries(f"{repository}: enumerate", enumerate_snapshots)
    matches = [s for s in snapshots if isinstance(s, str) and _series(s) == series]
    if not matches:
        fail(f"{repository}: the mirror enumerates no {series!r} snapshots")
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


def _declared_signing_keys(buck: str, repositories: list[str]) -> dict[str, str]:
    """Where each signing key the given repositories declare is fetched from, by fingerprint."""
    out = buck_output(
        buck,
        "uquery",
        "--json",
        "--output-attribute=^signing_keys$",
        f"set({' '.join(repositories)})",
    )
    keys: dict[str, str] = {}
    for target, attributes in json.loads(out).items():
        for fingerprint, url in (attributes.get("signing_keys") or {}).items():
            if keys.setdefault(fingerprint, url) != url:
                fail(
                    f"catalog: {target} fetches key {fingerprint} from {url}, "
                    f"another repository from {keys[fingerprint]}"
                )
    return keys


def _fetch_signing_key(fingerprint: str, url: str) -> str:
    """One armored public key, normalized to end in exactly one newline."""
    print(f"==> fetching signing key {fingerprint}", file=sys.stderr)

    def fetch() -> bytes:
        with urlopen(url, agent="tine-catalog") as response:
            return response.read()

    raw = with_retries(f"key {fingerprint}", fetch)
    # rpm imports armored keys only; a binary keyring such as fedoraproject.org's fedora.gpg is not one.
    if not raw.startswith(ARMOR_HEADER):
        fail(f"catalog: {url} is not an armored public key")
    return raw.decode("ascii").rstrip("\n") + "\n"


def _signing_keys(
    buck: str,
    catalog_dir: Path,
    repositories: list[str],
) -> Iterator[tuple[Path, str]]:
    """Fetch the declared signing keys the catalog does not hold yet."""
    for fingerprint, url in sorted(_declared_signing_keys(buck, repositories).items()):
        path = catalog_dir / SIGNING_KEY_DIRECTORY / (fingerprint + SIGNING_KEY_SUFFIX)
        if not path.exists():
            yield path, _fetch_signing_key(fingerprint, url)


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
        fail(f"catalog: box names selected more than once: {duplicates}")
    by_name = {_name_of(target): target for target in all_resolves}
    unknown = sorted(set(selected_boxes) - by_name.keys())
    if unknown:
        fail(f"catalog: unknown box names: {unknown}")
    selected = set(selected_boxes)
    return [target for target in all_resolves if _name_of(target) in selected]


def _repositories_for_boxes(buck: str, boxes: list[str]) -> list[str]:
    """Remote repository targets reachable from the given box targets."""
    box_set = " ".join(boxes)
    query = f"attrfilter(labels, '{REMOTE_REPOSITORY_LABEL}', deps(set({box_set})))"
    return sorted(buck_output(buck, "uquery", query).split())


def _plan(
    buck: str,
    catalog: str,
    selected_boxes: list[str] | None,
    advance_snapshots: bool,
) -> tuple[Path, list[str], list[str]]:
    """Pick what to refresh, advancing the mirror pins it will be resolved against.

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
        fail(f"catalog: no repository/box refresh targets found in {catalog}")
    catalog_dir = _catalog_directory(buck, targets)
    if advance_snapshots:
        _advance_snapshots(buck, catalog, catalog_dir, snapshots)
    return catalog_dir, snapshots, resolves


def _regenerate(
    buck: str,
    catalog_dir: Path,
    snapshots: list[str],
    resolves: list[str],
) -> Iterator[tuple[Path, str]]:
    """Fetch keys, snapshot repositories and resolve boxes, yielding each result and where it belongs.

    Nothing is written here, so a verify regenerates through the same commands a refresh does and
    still leaves the checkout exactly as it found it.
    """
    yield from _signing_keys(buck, catalog_dir, snapshots)

    for target in snapshots:
        yield catalog_dir / _repository_snapshot_path(target), _snapshot(buck, target)

    for target in resolves:
        yield catalog_dir / _box_snapshot_path(target), _resolve(buck, target)


def _commit(catalog_dir: Path) -> None:
    """Commit the refreshed catalog, pins, keys and snapshots alike.

    Scoped to the catalog directory rather than the files just written: advancing a pin rewrites
    the declaration too, and a repository snapshotted for the first time is not tracked yet.
    """
    if not commit_paths(catalog_dir, ".", "catalog: Refresh pinned snapshots and box locks"):
        print("==> the catalog is already up to date, nothing to commit", file=sys.stderr)


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
        default=nested_buck(),
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
    p.add_argument(
        "--commit",
        action="store_true",
        help="commit the refreshed catalog",
    )
    args = p.parse_args(argv)
    if args.commit and args.verify:
        p.error("--verify leaves the checkout as it found it, so there is nothing to commit")
    catalog = _catalog_pattern(args.catalog)

    # Run nested commands from the project root so wrappers resolve consistently.
    with contextlib.chdir(buck_output(args.buck, "root", "--kind", "project")) as _:
        catalog_dir, snapshots, resolves = _plan(
            args.buck, catalog, args.box, advance_snapshots=not args.verify
        )
        regenerated = _regenerate(args.buck, catalog_dir, snapshots, resolves)
        if not args.verify:
            for path, content in regenerated:
                atomic_write_text(path, content)
            if args.commit:
                _commit(catalog_dir)
            return

        print("==> verifying the committed catalog matches", file=sys.stderr)
        stale = [report for path, content in regenerated if (report := _differences(path, content))]

    if stale:
        for report in stale:
            print(report, end="", file=sys.stderr)
        fail("catalog: the committed catalog is not what the pinned resolvers produce")


if __name__ == "__main__":
    main()
