"""Pin a repository's build metadata and authoritative RPM inventory.

Refresh runs this host-side fetch-and-filter step independently per repository.
The snapshot contains filtered repomd, pinned streams, and a pkgid-keyed package index.
"""

import argparse
import bz2
import compression.zstd
import gzip
import hashlib
import http.client
import json
import lzma
import string
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import Protocol, TypedDict
from urllib.parse import unquote, urlsplit

from util import atomic_text_writer

_REPOMD_NS = "http://linux.duke.edu/metadata/repo"
_PRIMARY_NS = "http://linux.duke.edu/metadata/common"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
# Keep only streams needed for dependencies, path providers, and package groups.
_REQUIRED_STREAMS = ("primary", "filelists")
_OPTIONAL_STREAMS = ("group",)
_KEPT_STREAMS = frozenset(_REQUIRED_STREAMS + _OPTIONAL_STREAMS)


class PackageEntry(TypedDict):
    location: str
    size: int


class RepositoryStream(TypedDict):
    out: str
    url: str
    sha256: str
    size: int


class RepositorySnapshot(TypedDict):
    packages: dict[str, PackageEntry]
    repomd: str
    streams: list[RepositoryStream]


class RepositoryManifest(TypedDict):
    id: str
    baseurl: str


class BinaryReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


def _urlopen(url: str) -> http.client.HTTPResponse:
    # CDN bot filters (e.g. Cloudflare's) reject Python's default Python-urllib agent.
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "tine-snapshot"}))


_TRANSIENT_HTTP_STATUS = frozenset((408, 429, 500, 502, 503, 504))
_FETCH_ATTEMPTS = 4


def _with_retries[T](what: str, operation: Callable[[], T]) -> T:
    """Run one network operation, retrying transient connection failures and HTTP errors."""
    for attempt in range(1, _FETCH_ATTEMPTS + 1):
        try:
            return operation()
        except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
            permanent = (
                isinstance(error, urllib.error.HTTPError) and error.code not in _TRANSIENT_HTTP_STATUS
            )
            if permanent or attempt == _FETCH_ATTEMPTS:
                raise
            print(f"{what}: {error}; retrying…", file=sys.stderr)
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def _relative_href(rid: str, what: str, href: str | None) -> str:
    """Validate a repository-owned URL path before joining it to the base URL."""
    if not href:
        raise SystemExit(f"{rid}: {what} has an empty location")
    parsed = urlsplit(href)

    # Decode to a fixed point so nested escapes cannot conceal traversal or separators.
    decoded = href
    while True:
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded

    parts = decoded.split("/")
    external = bool(parsed.scheme or parsed.netloc or parsed.query or parsed.fragment)
    invalid_path = (
        decoded.startswith("/")
        or decoded.endswith("/")
        # Reject encoded slashes that would change the path after URL handling.
        or decoded.count("/") != href.count("/")
        or any(part in ("", ".", "..") for part in parts)
        or "\\" in decoded
    )
    non_ascii = any(ord(character) > 127 for character in href)
    control_character = any(ord(character) < 32 or ord(character) == 127 for character in decoded)
    if external or invalid_path or non_ascii or control_character:
        raise SystemExit(f"{rid}: {what} has unsupported location {href!r}")
    return href


def _location_href(rid: str, what: str, location: ET.Element | None) -> str:
    """Read a location that is relative to the repository root."""
    href = location.get("href") if location is not None else None
    location_base = (
        None if location is None else (location.get("base") or location.get(f"{{{_XML_NS}}}base"))
    )
    if location_base is not None:
        raise SystemExit(f"{rid}: {what} has unsupported location {href!r}")
    return _relative_href(rid, what, href)


def _sha256(rid: str, what: str, value: str | None) -> str:
    """Normalize and validate a sha256 supplied by repository metadata."""
    digest = "" if value is None else value.strip().lower()
    if len(digest) != 64 or any(character not in string.hexdigits for character in digest):
        raise SystemExit(f"{rid}: {what} has invalid sha256 {digest!r}")
    return digest


def _metadata_int(rid: str, what: str, value: str | None, *, minimum: int) -> int:
    """Parse one bounded metadata integer with a useful repository-scoped error."""
    try:
        number = int(value) if value is not None else minimum - 1
    except ValueError:
        number = minimum - 1
    if number < minimum:
        raise SystemExit(f"{rid}: {what} has invalid size/count {value!r}")
    return number


def _parse_primary(rid: str, source: BinaryReader) -> dict[str, PackageEntry]:
    """Stream a primary XML file into the snapshot's compact pkgid-keyed package map."""
    events = ET.iterparse(source, events=("start", "end"))
    _, root = next(events)
    if root.tag != f"{{{_PRIMARY_NS}}}metadata":
        raise SystemExit(f"{rid}: primary metadata has unexpected root {root.tag!r}")
    declared = root.get("packages")
    if declared is None:
        raise SystemExit(f"{rid}: primary metadata root lacks its package count")
    expected = _metadata_int(rid, "primary metadata package count", declared, minimum=0)

    packages: dict[str, PackageEntry] = {}
    count = 0
    package_tag = f"{{{_PRIMARY_NS}}}package"
    for event, element in events:
        if event != "end" or element.tag != package_tag:
            continue

        count += 1
        checksum = element.find(f"{{{_PRIMARY_NS}}}checksum")
        location = element.find(f"{{{_PRIMARY_NS}}}location")
        size = element.find(f"{{{_PRIMARY_NS}}}size")
        if checksum is None or checksum.text is None:
            raise SystemExit(f"{rid}: primary package {count} lacks a checksum")
        if checksum.get("type") != "sha256" or checksum.get("pkgid") != "YES":
            raise SystemExit(f"{rid}: primary package {count} does not have a sha256 pkgid")
        pkgid = _sha256(rid, f"primary package {count} pkgid", checksum.text)

        href = _location_href(rid, f"primary package {pkgid}", location)

        package_size = size.get("package") if size is not None else None
        download_size = _metadata_int(rid, f"primary package {pkgid}", package_size, minimum=1)
        entry = PackageEntry(location=href, size=download_size)
        if previous := packages.get(pkgid):
            if previous["size"] != entry["size"]:
                raise SystemExit(f"{rid}: duplicate pkgid {pkgid} has conflicting sizes")
            # Mirrors sometimes expose identical content at more than one path.
            previous["location"] = min(str(previous["location"]), href)
        else:
            packages[pkgid] = entry
        element.clear()

    if count != expected:
        raise SystemExit(f"{rid}: primary metadata declared {expected} packages but contained {count}")
    return packages


def _load_package_index(rid: str, stream: RepositoryStream) -> dict[str, PackageEntry]:
    """Download, verify, decompress, and parse the pinned primary stream."""
    expected_size = int(stream["size"])
    with tempfile.TemporaryFile("w+b") as compressed:

        def download() -> None:
            # A retried attempt restarts the stream from scratch.
            compressed.seek(0)
            compressed.truncate()
            digest = hashlib.sha256()
            total = 0
            with _urlopen(str(stream["url"])) as response:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    # Stop before writing unbounded data from a stale or malicious endpoint.
                    if total > expected_size:
                        raise SystemExit(
                            f"{rid}: primary stream size {total} does not match repomd size {expected_size}"
                        )
                    digest.update(chunk)
                    compressed.write(chunk)
            if total != expected_size:
                raise SystemExit(
                    f"{rid}: primary stream size {total} does not match repomd size {expected_size}"
                )
            if digest.hexdigest() != stream["sha256"]:
                raise SystemExit(f"{rid}: primary stream checksum does not match repomd")

        _with_retries(f"{rid}: {stream['out']}", download)

        compressed.seek(0)
        magic = compressed.read(6)
        compressed.seek(0)
        # Metadata names are not authoritative; select the decoder from file contents.
        with ExitStack() as stack:
            if magic.startswith(b"\x28\xb5\x2f\xfd"):
                source = stack.enter_context(compression.zstd.ZstdFile(compressed, mode="rb"))
            elif magic.startswith(b"\x1f\x8b"):
                source = stack.enter_context(gzip.GzipFile(fileobj=compressed, mode="rb"))
            elif magic.startswith(b"\xfd7zXZ\x00"):
                source = stack.enter_context(lzma.LZMAFile(compressed, mode="rb"))
            elif magic.startswith(b"BZh"):
                source = stack.enter_context(bz2.BZ2File(compressed, mode="rb"))
            elif magic.lstrip().startswith(b"<"):
                source = compressed
            else:
                raise SystemExit(f"{rid}: unsupported primary compression (magic {magic.hex()})")
            return _parse_primary(rid, source)


def _repository_stream(
    rid: str,
    baseurl: str,
    stream_type: str,
    data: ET.Element,
) -> RepositoryStream:
    """Decode and validate one retained repomd data record."""
    location = data.find(f"{{{_REPOMD_NS}}}location")
    checksum = data.find(f"{{{_REPOMD_NS}}}checksum[@type='sha256']")
    size = data.find(f"{{{_REPOMD_NS}}}size")
    if (
        location is None
        or location.get("href") is None
        or checksum is None
        or checksum.text is None
        or size is None
        or size.text is None
    ):
        raise SystemExit(f"{rid}: {stream_type} record lacks a location, sha256 checksum, or size")

    href = _location_href(rid, f"{stream_type} stream", location)
    return RepositoryStream(
        out=PurePosixPath(href).name,
        url=baseurl + href,
        sha256=_sha256(rid, f"{stream_type} stream", checksum.text),
        # Recording the compressed size avoids an unpinned HEAD request later.
        size=_metadata_int(rid, f"{stream_type} stream", size.text, minimum=1),
    )


def snapshot_repodata(rid: str, baseurl: str) -> RepositorySnapshot:
    """Pin one repo's build-time repodata; see the module docstring for the shape."""
    base = baseurl.rstrip("/") + "/"

    def download_repomd() -> bytes:
        with _urlopen(base + "repodata/repomd.xml") as f:
            return f.read()

    repomd = _with_retries(f"{rid}: repomd.xml", download_repomd)

    ET.register_namespace("", _REPOMD_NS)  # Preserve the default namespace.
    root = ET.fromstring(repomd)

    kept: set[str] = set()
    streams: list[RepositoryStream] = []
    outputs: set[str] = set()
    primary: RepositoryStream | None = None
    # The filtered repomd becomes the exact local repository view consumed by libdnf5.
    for data in list(root.findall(f"{{{_REPOMD_NS}}}data")):
        stream_type = data.get("type")
        if stream_type not in _KEPT_STREAMS:
            root.remove(data)
            continue
        if stream_type in kept:
            raise SystemExit(f"{rid}: repomd.xml contains duplicate {stream_type!r} streams")
        kept.add(stream_type)

        stream = _repository_stream(rid, base, stream_type, data)
        if stream["out"] in outputs:
            raise SystemExit(f"{rid}: repomd.xml streams share output basename {stream['out']!r}")
        outputs.add(stream["out"])
        streams.append(stream)
        if stream_type == "primary":
            primary = stream

    if missing := [stream for stream in _REQUIRED_STREAMS if stream not in kept]:
        raise SystemExit(f"{rid}: repomd.xml missing {missing}")
    if primary is None:
        raise SystemExit(f"{rid}: repomd.xml has no primary stream")

    filtered = ET.tostring(root, encoding="unicode", xml_declaration=True)
    packages = _load_package_index(rid, primary)
    return {"packages": packages, "repomd": filtered, "streams": streams}


def _write_snapshot(path: Path, snapshot: RepositorySnapshot) -> None:
    """Atomically replace a snapshot with deterministic, reviewable UTF-8 JSON."""
    with atomic_text_writer(path) as output:
        # One package per line keeps this large generated file reviewable.
        output.write('{\n  "packages": {\n')
        packages = sorted(snapshot["packages"].items())
        for index, (pkgid, package) in enumerate(packages):
            comma = "," if index + 1 < len(packages) else ""
            output.write(
                "    "
                + json.dumps(pkgid)
                + ": "
                + json.dumps(package, sort_keys=True, separators=(",", ":"))
                + comma
                + "\n"
            )
        output.write('  },\n  "repomd": ')
        json.dump(snapshot["repomd"], output)
        output.write(',\n  "streams": ')
        streams = json.dumps(snapshot["streams"], indent=2, sort_keys=True)
        output.write(streams.replace("\n", "\n  "))
        output.write("\n}\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="snapshot")
    parser.add_argument(
        "--manifest",
        required=True,
        help="the repository's manifest ({id, baseurl} JSON, from its [manifest] sub-target)",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="snapshot path to write ({packages, repomd, streams} JSON)",
    )
    args = parser.parse_args(argv)

    manifest: RepositoryManifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    print(f"{manifest['id']}: snapshotting repodata…", file=sys.stderr)
    snapshot = snapshot_repodata(manifest["id"], manifest["baseurl"])
    out = Path(args.out)
    _write_snapshot(out, snapshot)
    print(f"wrote {out} ({len(snapshot['packages'])} packages)", file=sys.stderr)


if __name__ == "__main__":
    main()
