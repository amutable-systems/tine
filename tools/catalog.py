# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Refresh pure catalog snapshots, then resolve box transactions against those pins.

With `--advance`, repositories pinned to a mirror that publishes snapshots first advance their
declaration to the newest one: an rpmrepo gateway enumerates them, the Arch Linux Archive publishes
a tree per day and records when it last finished one, and the Debian archive lists every timestamp
it holds. Repositories sharing one pin advance together, and rolling back means editing the pin.

Remote box-lock entries retain their package transports, so a repository's package pool keeps
the committed box available after its repodata advances.

A repository's declared signing keys are fetched once, by fingerprint, and the file is never
rewritten afterwards. Whether a file is the declared key is the build's check, with the package
system's own tools.

The host orchestrator discovers refresh targets and takes each result from the driver's stdout,
so refreshing and verifying run one command and only the host decides where an output belongs.
Nested Buck reuses the invoking daemon through the inherited isolation directory.
"""

import argparse
import base64
import contextlib
import difflib
import functools
import itertools
import json
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
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
BOX_LOCK_LABEL = "tine:box-lock"
# Keep in sync with the lock targets `box.new` declares in box/build.bzl.
BOX_LOCK_INFIX = ".lock."
REMOTE_REPOSITORY_LABEL = "tine:remote-repository"
RPM_REMOTE_REPOSITORY_LABEL = "tine:rpm-remote-repository"
PACMAN_REMOTE_REPOSITORY_LABEL = "tine:pacman-remote-repository"
DEB_REMOTE_REPOSITORY_LABEL = "tine:deb-remote-repository"

# Keep in sync with SIGNING_KEY_DIRECTORY/SIGNING_KEY_SUFFIX in package/repository.bzl.
SIGNING_KEY_DIRECTORY = Path("snapshot/key")
SIGNING_KEY_SUFFIX = ".key"

ARMOR_HEADER = b"-----BEGIN PGP PUBLIC KEY BLOCK-----"
ARMOR_FOOTER = b"-----END PGP PUBLIC KEY BLOCK-----"
# How a binary OpenPGP stream that opens with a public-key packet starts: tag 6 in either framing.
_PUBLIC_KEY_PACKET = (0x98, 0x99, 0x9A, 0xC6)


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


def _lock_path(kind: str, stem: str, architecture: str) -> Path:
    """Where one architecture's committed lock lives, relative to the catalog package.

    Keep in sync with `_repository_lock_path`/`box_lock_path` in package/repository.bzl, which read it.
    """
    return Path("snapshot") / kind / f"{stem}.{architecture}.json"


def _repository_lock_path(target: str, architecture: str) -> Path:
    name = _name_of(target)
    if not name.endswith(".repository"):
        fail(f"catalog: {target} does not end with '.repository'")
    return _lock_path("repo", name.removesuffix(".repository"), architecture)


def _box_lock_parts(target: str) -> tuple[str, str]:
    """The box a `<box>.lock.<architecture>` target resolves, and the architecture it resolves for."""
    # An architecture name has no dot in it, so the last infix is the one.
    box, separator, architecture = _name_of(target).rpartition(BOX_LOCK_INFIX)
    if not separator or not box.endswith(".box"):
        fail(f"catalog: {target} is not a <name>.box{BOX_LOCK_INFIX}<architecture> target")
    return box, architecture


def _box_lock_path(target: str) -> Path:
    box, architecture = _box_lock_parts(target)
    return _lock_path("box", box.removesuffix(".box"), architecture)


def _run(buck: str, target: str) -> str:
    """Run a refresh target, returning what it wrote to stdout.

    Buck execs the target rather than piping it, and its own output is on stderr, so stdout is
    the driver's alone. That is the only channel out: the hermetic sandbox a driver runs in binds
    the project and nothing else, so a path outside it would land in the sandbox's own tmpfs.
    """
    # Mute nested Buck while preserving driver progress on stderr.
    command = [buck, "-v", "0", "run", target, "--console", "none", "--", "--out", "-"]
    return subprocess.run(command, check=True, stdout=subprocess.PIPE, encoding="utf-8").stdout


@dataclass(frozen=True)
class Pin:
    """One repository's mirror pin, as the catalog declares it."""

    target: str
    mirror: str
    # Exactly as the catalog writes it, `$basearch` included.
    snapshot: str
    # What the mirror serves, and so what the pin names one snapshot of each.
    architectures: tuple[str, ...]
    # The rest of the package system's own namespace, for a mirror that needs more than its URL to
    # be asked what it published. Out of the comparison because a dict cannot be hashed.
    metadata: Mapping[str, str] = field(default_factory=dict, compare=False)

    @property
    def name(self) -> str:
        """The repository's target name, as progress and errors report it."""
        return _name_of(self.target)


def _pinned_repositories(buck: str, catalog: str, label: str, prefix: str) -> list[Pin]:
    """The repositories in `catalog` whose base URLs come from a `<prefix>.*` mirror pin."""
    out = buck_output(
        buck,
        "uquery",
        "--json",
        "--output-attribute=^(architectures|metadata)$",
        f"attrfilter(labels, '{label}', {catalog})",
    )
    pins = []
    for target, attributes in sorted(json.loads(out).items()):
        metadata = attributes.get("metadata") or {}
        pin = {
            key.removeprefix(f"{prefix}."): value
            for key, value in metadata.items()
            if key.startswith(f"{prefix}.")
        }
        if "snapshot" in pin and "mirror" in pin:
            pins.append(
                Pin(
                    target=target,
                    mirror=pin["mirror"],
                    snapshot=pin["snapshot"],
                    architectures=tuple(attributes["architectures"]),
                    metadata=pin,
                )
            )
    return pins


class _Checkout:
    """The files a refresh writes, and what they held before, so a failed one puts them back.

    A refresh has to write as it goes: the advanced pin is what the snapshots are taken at, and a
    snapshot has to be on disk before the box that reads it resolves. Left half done, the advanced
    pin beside the old snapshots builds neither the old catalog nor the new, so a failure restores
    every file to what the checkout held.
    """

    def __init__(self) -> None:
        self._originals: dict[Path, str | None] = {}

    def write(self, path: Path, content: str) -> None:
        if path not in self._originals:
            self._originals[path] = path.read_text(encoding="utf-8") if path.exists() else None
        atomic_write_text(path, content)

    def restore(self) -> None:
        """Put every written file back, all of them even if one refuses, then report the first refusal."""
        failed: Exception | None = None
        for path, content in self._originals.items():
            try:
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write_text(path, content)
            except OSError as error:
                failed = failed or error
        self._originals.clear()
        if failed is not None:
            raise failed


def _served_architectures(buck: str, repositories: list[str]) -> dict[str, tuple[str, ...]]:
    """The architectures each repository's mirror serves, and so the locks it keeps."""
    out = buck_output(
        buck,
        "uquery",
        "--json",
        "--output-attribute=^architectures$",
        f"set({' '.join(repositories)})",
    )
    return {
        target: tuple(sorted(attributes["architectures"])) for target, attributes in json.loads(out).items()
    }


def _rewrite_pin(checkout: _Checkout, declaration: Path, attribute: str, current: str, wanted: str) -> None:
    """Repoint every declaration carrying one pin at its successor.

    Every occurrence moves, which is why only a whole pin group is ever advanced: repositories
    written from one literal cannot be moved apart by rewriting it.
    """
    pin = f'{attribute} = "{current}"'
    content = declaration.read_text(encoding="utf-8")
    if pin not in content:
        fail(f"catalog: expected {pin!r} in {declaration}")
    checkout.write(declaration, content.replace(pin, f'{attribute} = "{wanted}"'))


def _advance(
    checkout: _Checkout,
    repositories: list[Pin],
    selected: set[str],
    newest: Callable[[list[Pin]], str],
    declaration: Path,
    attribute: str,
) -> int:
    """Advance each distinct pin once, so repositories sharing one stay on one snapshot; how many moved."""
    groups: dict[str, list[Pin]] = {}
    for pin in repositories:
        groups.setdefault(pin.snapshot, []).append(pin)

    advances = {}
    for current, pins in sorted(groups.items()):
        # Grouping is over the whole catalog, not the selection, because rewriting the literal
        # moves every repository written from it. A group the caller is not about to re-snapshot
        # in full therefore has to stay where it is.
        names = ", ".join(pin.name for pin in pins)
        if not {pin.target for pin in pins} <= selected:
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
        _rewrite_pin(checkout, declaration, attribute, current, wanted)
    return len(advances)


def _series(snapshot: str) -> str:
    """A snapshot id's series: everything before the trailing datestamp (rpmrepo's naming)."""
    return snapshot.rsplit("-", 1)[0]


def _datestamp(snapshot: str) -> str:
    """A snapshot id's trailing datestamp."""
    return snapshot.rsplit("-", 1)[1]


def _pinned_snapshot(buck: str, pin: Pin, architecture: str) -> str:
    """The snapshot id one architecture of a pinned repository is served from, as the mirror spells it.

    The rule that expands the pin's placeholder writes it into that architecture's snapshot spec,
    so this reads it from there.
    """
    # Keep the subtarget name in sync with `_manifest_subtarget` in package/repository.bzl.
    subtarget = f"{pin.target}[manifest.{architecture}]"
    manifest = buck_output(buck, "build", "--show-full-simple-output", subtarget)
    spec = cast(dict[str, str], json.loads(Path(manifest).read_text(encoding="utf-8")))
    return spec["baseurl"].rsplit("/", 1)[1]


def _newest_rpmrepo_snapshot(buck: str, pins: list[Pin]) -> str:
    """The newest snapshot every repository and architecture sharing one rpmrepo pin can move to.

    The answer is a pin like the one read, placeholder and all: a refresh moves the datestamp and
    leaves the shape of the id the catalog maintains alone.
    """
    offers = {
        f"{pin.name} ({architecture})": _newest_snapshot(
            pin.name, pin.mirror, _series(_pinned_snapshot(buck, pin, architecture))
        )
        for pin in pins
        for architecture in pin.architectures
    }
    # A group shares its snapshot literal, so any member's series is the series of all of them.
    wanted = {f"{_series(pins[0].snapshot)}-{_datestamp(offer)}" for offer in offers.values()}
    if len(wanted) != 1:
        detail = ", ".join(f"{who} offers {offer}" for who, offer in sorted(offers.items()))
        fail(f"catalog: one pin cannot advance to several snapshots: {detail}")
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


def _newest_archive_snapshot(pins: list[Pin]) -> str:
    """The newest day the archive has finished publishing.

    The archive publishes a tree per day rather than an index to enumerate, but it does record when
    it last finished one, which is the only thing that says a day is complete rather than half
    written. Every repository in a group shares the pin, so one marker answers for all of them.
    """
    pin = pins[0]
    # Keep in sync with pacman_remote_repository's base URL, whose mirror this is rooted at.
    url = pin.mirror.rstrip("/") + "/last/lastsync"

    def lastsync() -> str:
        with urlopen(url, agent="tine-catalog") as response:
            synced = datetime.fromtimestamp(int(response.read().strip()), UTC)
        return synced.strftime("%Y/%m/%d")

    # An advance never moves a pin backwards.
    return max(with_retries("archive: lastsync", lastsync), pin.snapshot)


def _newest_debian_snapshot(pins: list[Pin]) -> str:
    """The newest timestamp the Debian archive has published.

    The archive publishes a tree per timestamp rather than an index to enumerate, and lists every
    one it holds under a machine-readable endpoint of its own; the last is the newest. Every
    component in a group shares the pin, so one listing answers for all of them.
    """
    pin = pins[0]
    archive = pin.metadata["archive"]
    # Keep in sync with deb_remote_repository's base URL, whose mirror this is rooted at. The
    # trailing slash is load-bearing: without it the archive answers 308 to a plain `http://` of
    # the same path, and urllib follows a redirect wherever it points.
    url = pin.mirror.rstrip("/").removesuffix("/archive") + "/mr/timestamp/"

    def timestamps() -> str:
        with urlopen(url, agent="tine-catalog") as response:
            published = json.load(response)["result"][archive]
        # A timestamp names a tree the archive has finished writing, so unlike the Arch mirror
        # there is no separate marker that says the newest one is complete.
        offered = [stamp for stamp in published if isinstance(stamp, str)]
        if not offered:
            fail(f"archive: {url} lists no {archive} snapshots")
        return max(offered)

    # An advance never moves a pin backwards.
    return max(with_retries("archive: timestamps", timestamps), pin.snapshot)


def _advance_snapshots(
    checkout: _Checkout, buck: str, catalog: str, catalog_dir: Path, selected: list[str]
) -> None:
    """Advance the selected mirror-pinned repositories to the newest snapshot their mirror offers.

    Advancing a pin without re-snapshotting the repository it belongs to would leave a base URL
    from one day composing package locations from another, so this stays inside the selection the
    caller is about to snapshot.
    """
    declaration = catalog_dir / "BUCK"
    wanted = set(selected)
    advanced = 0
    for label, prefix, attribute, newest in (
        (
            RPM_REMOTE_REPOSITORY_LABEL,
            "rpmrepo",
            "rpmrepo_snapshot",
            functools.partial(_newest_rpmrepo_snapshot, buck),
        ),
        (PACMAN_REMOTE_REPOSITORY_LABEL, "archlinux", "archive_snapshot", _newest_archive_snapshot),
        (DEB_REMOTE_REPOSITORY_LABEL, "debian", "archive_snapshot", _newest_debian_snapshot),
    ):
        pinned = _pinned_repositories(buck, catalog, label, prefix)
        advanced += _advance(checkout, pinned, wanted, newest, declaration, attribute)
    if not advanced:
        # Asked for and not done is worth a line: every selected pin is either newest already or
        # shared with a repository outside the selection.
        print("==> no pin advanced", file=sys.stderr)


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


def _crc24(data: bytes) -> int:
    """RFC 9580's CRC-24, the check an armored block ends with."""
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def armor(raw: bytes) -> str:
    """A binary public key as the armored block the keyring drivers import."""
    body = base64.b64encode(raw).decode("ascii")
    lines = [body[start : start + 64] for start in range(0, len(body), 64)]
    check = base64.b64encode(_crc24(raw).to_bytes(3, "big")).decode("ascii")
    return (
        "\n".join([ARMOR_HEADER.decode("ascii"), "", *lines, f"={check}", ARMOR_FOOTER.decode("ascii")])
        + "\n"
    )


def _fetch_signing_key(fingerprint: str, url: str) -> str:
    """One armored public key, normalized to end in exactly one newline."""
    print(f"==> fetching signing key {fingerprint}", file=sys.stderr)

    def fetch() -> bytes:
        with urlopen(url, agent="tine-catalog") as response:
            return response.read()

    raw = with_retries(f"key {fingerprint}", fetch)
    # rpm imports armored keys only, and a web key directory serves a key binary: armor those here,
    # so that the catalog holds one form. A binary keyring such as fedoraproject.org's fedora.gpg
    # starts the same way and is caught by the build's check that a file holds its declared key.
    if raw.startswith(ARMOR_HEADER):
        return raw.decode("ascii").rstrip("\n") + "\n"
    if raw[:1] and raw[0] in _PUBLIC_KEY_PACKET:
        return armor(raw)
    fail(f"catalog: {url} is not an OpenPGP public key, armored or binary")


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


def _snapshot(buck: str, target: str, architecture: str) -> str:
    """One repository's current pure metadata, for one architecture.

    Keep the subtarget name in sync with `_snapshot_subtarget` in package/repository.bzl.
    """
    subtarget = f"{target}[snapshot.{architecture}]"
    print(f"==> snapshotting {_name_of(target)} for {architecture} (via {subtarget})", file=sys.stderr)
    return _run(buck, subtarget)


def _resolve(buck: str, target: str) -> str:
    """What one box is made of, for the architecture its lock target resolves."""
    box, architecture = _box_lock_parts(target)
    print(f"==> resolving {box} for {architecture} (via {target})", file=sys.stderr)
    return _run(buck, target)


def _select_boxes(all_resolves: list[str], selected_boxes: list[str] | None) -> list[str]:
    """The lock targets of the selected boxes, every architecture of each."""
    if selected_boxes is None:
        return all_resolves
    duplicates = sorted({name for name in selected_boxes if selected_boxes.count(name) > 1})
    if duplicates:
        fail(f"catalog: box names selected more than once: {duplicates}")
    boxes = {_box_lock_parts(target)[0] for target in all_resolves}
    unknown = sorted(set(selected_boxes) - boxes)
    if unknown:
        fail(f"catalog: boxes {unknown} have no lock targets: unknown, or declaring no architectures")
    selected = set(selected_boxes)
    return [target for target in all_resolves if _box_lock_parts(target)[0] in selected]


def _repositories_for_boxes(buck: str, boxes: list[str]) -> list[str]:
    """Remote repository targets reachable from the given box targets."""
    box_set = " ".join(boxes)
    query = f"attrfilter(labels, '{REMOTE_REPOSITORY_LABEL}', deps(set({box_set})))"
    return sorted(buck_output(buck, "uquery", query).split())


def _plan(
    buck: str,
    catalog: str,
    selected_boxes: list[str] | None,
) -> tuple[Path, list[str], list[str]]:
    """Pick what to refresh.

    Selecting boxes also scopes the snapshotted repositories to those the boxes depend on, so a
    partial refresh or verify never touches a repository outside the selection.
    """
    all_resolves = _targets_with_label(buck, catalog, BOX_LOCK_LABEL)
    resolves = _select_boxes(all_resolves, selected_boxes)

    if selected_boxes is None:
        snapshots = _targets_with_label(buck, catalog, REMOTE_REPOSITORY_LABEL)
    else:
        snapshots = _repositories_for_boxes(buck, resolves)
    targets = all_resolves + snapshots
    if not targets:
        fail(f"catalog: no repository/box refresh targets found in {catalog}")
    return _catalog_directory(buck, targets), snapshots, resolves


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

    served = _served_architectures(buck, snapshots)
    for target in snapshots:
        for architecture in served[target]:
            yield (
                catalog_dir / _repository_lock_path(target, architecture),
                _snapshot(buck, target, architecture),
            )

    for target in resolves:
        yield catalog_dir / _box_lock_path(target), _resolve(buck, target)


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
        "--advance",
        action="store_true",
        help="first advance the selected repositories' pins to the newest snapshot their mirrors offer",
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
    if args.advance and args.verify:
        p.error("--verify checks the committed pins, so there is nothing to advance")
    catalog = _catalog_pattern(args.catalog)

    # Run nested commands from the project root so wrappers resolve consistently.
    with contextlib.chdir(buck_output(args.buck, "root", "--kind", "project")) as _:
        catalog_dir, snapshots, resolves = _plan(args.buck, catalog, args.box)
        # Lazy: nothing is snapshotted until the loop below asks, after the pins have moved.
        regenerated = _regenerate(args.buck, catalog_dir, snapshots, resolves)
        if not args.verify:
            checkout = _Checkout()
            try:
                if args.advance:
                    _advance_snapshots(checkout, args.buck, catalog, catalog_dir, snapshots)
                for path, content in regenerated:
                    checkout.write(path, content)
            except BaseException:
                print("==> the refresh failed; restoring the checkout", file=sys.stderr)
                checkout.restore()
                raise
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
