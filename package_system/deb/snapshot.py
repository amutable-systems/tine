#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Pin a Debian repository's Packages index and the packages that index describes.

A Debian archive describes itself twice over: a `Release` states the checksum of every index it
serves, and each index states the checksum of every package. The snapshot follows that chain once,
at refresh time, and keeps only its ends: the one index a solve reads, and the inventory every
package is fetched by. The suite layout is flattened away here, because what a build materializes
is the flat repository the planner stages for APT, not a mirror.

The signed `Release` is pinned beside the index rather than checked here: verifying it takes a
keyring, and the host contract is a pinned Buck and a pinned Python. The build's verifier follows
the same chain from that pinned file down to each package it installs.
"""

import io
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

import deb822
import release
import util

import snapshotter
from href import relative_href
from snapshotter import MetadataFile, PackageEntry, RepositoryMetadata


class Spec(snapshotter.Spec):
    suite: str
    component: str
    arch: str


def read_release(rid: str, base: str) -> tuple[MetadataFile, dict[str, str]]:
    """Pin the suite's signed Release and read the one stanza it carries.

    Read from the signed file rather than the plain `Release` beside it, so that what the pin
    states and what the build verifies are one document.
    """
    what = f"{rid}: {release.INRELEASE}"
    url = f"{base}/{release.INRELEASE}"
    with io.BytesIO() as raw:
        sha256, size = snapshotter.download(rid, release.INRELEASE, url, raw)
        message = release.unsigned(what, raw.getvalue())
    signed = MetadataFile(out=release.INRELEASE, url=url, sha256=sha256, size=size)
    return signed, release.stanza(what, message)


def _offered(rid: str, stanza: dict[str, str], field: str, wanted: str) -> None:
    """Check the Release itself says it carries what this repository was declared to pin."""
    offered = deb822.required(stanza, field.lower(), f"{rid}: Release").split()
    if wanted not in offered:
        util.fail(f"{rid}: Release offers {field} {' '.join(offered)}, not {wanted!r}")


def package_index(rid: str, stanza: dict[str, str], base: str, component: str, arch: str) -> MetadataFile:
    """Choose the index to pin, and the most immutable URL it can be fetched by.

    `Acquire-By-Hash` publishes an index under its own checksum, which is the only path on a live
    mirror that keeps naming the bytes this refresh saw once the suite has moved on. An archive
    snapshot is immutable either way, so taking it wherever it is offered costs nothing.
    """
    stated = release.stated(rid, stanza)
    directory = f"{component}/binary-{arch}"
    name = next((name for name in deb822.INDEX_NAMES if f"{directory}/{name}" in stated), None)
    if name is None:
        util.fail(f"{rid}: Release states no {directory}/Packages index")
    sha256, size = stated[f"{directory}/{name}"]
    if size <= 0:
        util.fail(f"{rid}: Release states an empty {directory}/{name}")
    by_hash = stanza.get("acquire-by-hash") == "yes"
    path = f"{directory}/by-hash/SHA256/{sha256}" if by_hash else f"{directory}/{name}"
    # `out` flattens the suite away: a materialized repository is the flat one APT is staged with.
    return MetadataFile(out=name, url=f"{base}/{path}", sha256=sha256, size=size)


def inventory(rid: str, index: Path) -> dict[str, PackageEntry]:
    """Index the packages the pinned Packages stream describes, by content checksum.

    A `Filename` is relative to the archive root rather than to the suite, which is why that root
    is the base URL a transaction composes its downloads against.
    """
    packages: dict[str, PackageEntry] = {}
    with deb822.open_text(index) as source:
        for position, stanza in enumerate(deb822.stanzas(source), 1):
            where = f"{rid}: Packages record {position}"
            what = f"package {deb822.required(stanza, 'package', where)}"
            location = relative_href(rid, what, stanza.get("filename"))
            if not location.endswith(".deb"):
                util.fail(f"{rid}: {what} is not a deb: {location!r}")
            snapshotter.add_package(
                packages,
                rid,
                snapshotter.checksum(rid, f"{what} SHA256", stanza.get("sha256")),
                PackageEntry(
                    location=location,
                    size=deb822.integer(
                        deb822.required(stanza, "size", what), f"{rid}: {what} Size", minimum=1
                    ),
                ),
            )
    return packages


def snapshot_repository(spec: Spec) -> Mapping[str, object]:
    """Pin one suite component's index and the packages it currently describes."""
    rid, suite, component, arch = spec["id"], spec["suite"], spec["component"], spec["arch"]
    print(f"{rid}: snapshotting {suite}/{component}/binary-{arch}…", file=sys.stderr)
    base = f"{spec['baseurl'].rstrip('/')}/dists/{relative_href(rid, 'suite', suite)}"
    signed, stanza = read_release(rid, base)
    _offered(rid, stanza, "Architectures", arch)
    _offered(rid, stanza, "Components", component)
    stream = package_index(rid, stanza, base, component, arch)

    with tempfile.TemporaryDirectory() as scratch:
        index = Path(scratch) / stream["out"]
        with index.open("wb") as raw:
            snapshotter.download(
                rid, "Packages", stream["url"], raw, size=stream["size"], sha256=stream["sha256"]
            )
        packages = inventory(rid, index)
    return {"metadata": RepositoryMetadata(files=[signed, stream], inline={}), "packages": packages}


def main(argv: list[str] | None = None) -> None:
    snapshotter.run("snapshot", Spec, snapshot_repository, argv)


if __name__ == "__main__":
    main()
