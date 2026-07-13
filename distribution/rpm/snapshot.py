"""snapshot — pin one repository's metadata and authoritative RPM inventory.

Run on refresh (via the repository target's `[snapshot]` sub-target, driven by the host
orchestrator //distribution:buckify), NOT during builds. A repository is a first-class
catalog citizen shared across distributions, so it pins independently of any of them —
and pinning is pure fetch-and-filter (urllib + ElementTree, no libdnf5), so unlike the
engine-closure resolve this runs on the host, needing no engine.

Reads the repository's manifest ({id, baseurl}, materialized from the catalog BUCK),
fetches `repodata/repomd.xml`, drops every <data> record except the build streams, and
writes one authoritative snapshot: filtered repomd, its pinned streams, and a pkgid-keyed
package index parsed from the pinned primary stream. Buck reconstructs `repodata/` from the
streams while the RPM pool declares one lazy raw artifact and its derived payload representation
per package. Re-running against an unchanged upstream reproduces the snapshot byte-for-byte.
"""

import argparse
import bz2
import compression.zstd
import gzip
import hashlib
import json
import lzma
import stat
import string
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

# repomd.xml's namespace (the <data>/<location>/<checksum> elements live here).
_REPOMD_NS = "http://linux.duke.edu/metadata/repo"
_PRIMARY_NS = "http://linux.duke.edu/metadata/common"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
# The repodata streams the build-time solve needs: primary (provides/requires) + filelists
# (file-path provides, for file-dep BuildRequires), plus group (comps) when the repo publishes
# it — plan expands `@group` installs (the buildroot base) against it at build time.
# other/updateinfo and the db/zck/xz re-encodings aren't needed and are dropped so libdnf5
# never tries to fetch them.
_BUILD_STREAMS = ("primary", "filelists")
_OPTIONAL_STREAMS = ("group",)


def _relative_href(rid: str, what: str, href: str | None) -> str:
    """Validate a repository-owned URL path before joining it to the base URL."""
    if not href:
        raise SystemExit(f"{rid}: {what} has an empty location")
    parsed = urlsplit(href)
    decoded = href
    while True:
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    parts = decoded.split("/")
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or decoded.startswith("/")
        or decoded.endswith("/")
        or decoded.count("/") != href.count("/")
        or any(part in ("", ".", "..") for part in parts)
        or "\\" in decoded
        or any(ord(character) > 127 for character in href)
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
    ):
        raise SystemExit(f"{rid}: {what} has unsupported location {href!r}")
    return href


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


def _parse_primary(rid: str, source) -> dict[str, dict[str, str | int]]:
    """Stream a primary XML file into the snapshot's compact pkgid-keyed package map."""
    packages: dict[str, dict[str, str | int]] = {}
    expected = None
    count = 0
    package_tag = f"{{{_PRIMARY_NS}}}package"
    for event, element in ET.iterparse(source, events=("start", "end")):
        if expected is None and event == "start":
            if element.tag != f"{{{_PRIMARY_NS}}}metadata":
                raise SystemExit(f"{rid}: primary metadata has unexpected root {element.tag!r}")
            declared = element.get("packages")
            if declared is None:
                raise SystemExit(f"{rid}: primary metadata root lacks its package count")
            expected = _metadata_int(rid, "primary metadata package count", declared, minimum=0)
            continue
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

        href = location.get("href") if location is not None else None
        location_base = (
            None if location is None else (location.get("base") or location.get(f"{{{_XML_NS}}}base"))
        )
        if location_base is not None:
            raise SystemExit(f"{rid}: primary package {pkgid} has unsupported location {href!r}")
        href = _relative_href(rid, f"primary package {pkgid}", href)

        package_size = size.get("package") if size is not None else None
        download_size = _metadata_int(rid, f"primary package {pkgid}", package_size, minimum=1)
        entry: dict[str, str | int] = {"location": href, "size": download_size}
        if previous := packages.get(pkgid):
            if previous["size"] != entry["size"]:
                raise SystemExit(f"{rid}: duplicate pkgid {pkgid} has conflicting sizes")
            previous["location"] = min(str(previous["location"]), href)
        else:
            packages[pkgid] = entry
        element.clear()

    if expected is None or count != expected:
        raise SystemExit(f"{rid}: primary metadata declared {expected} packages but contained {count}")
    return packages


def _snapshot_packages(rid: str, stream: dict[str, str | int]) -> dict[str, dict[str, str | int]]:
    """Download, verify, decompress, and parse the pinned primary stream."""
    digest = hashlib.sha256()
    expected_size = int(stream["size"])
    total = 0
    with tempfile.TemporaryFile("w+b") as compressed:
        with urllib.request.urlopen(str(stream["url"])) as response:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
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

        compressed.seek(0)
        magic = compressed.read(6)
        compressed.seek(0)
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


def snapshot_repodata(rid: str, baseurl: str) -> dict:
    """Pin one repo's build-time repodata; see the module docstring for the shape."""
    base = baseurl.rstrip("/") + "/"
    with urllib.request.urlopen(base + "repodata/repomd.xml") as f:
        repomd = f.read()

    ET.register_namespace("", _REPOMD_NS)  # keep the default (unprefixed) namespace on output
    root = ET.fromstring(repomd)

    kept = []
    streams = []
    outputs = set()
    primary = None
    for data in list(root.findall(f"{{{_REPOMD_NS}}}data")):
        stream_type = data.get("type")
        if stream_type not in _BUILD_STREAMS + _OPTIONAL_STREAMS:
            root.remove(data)  # drop other/updateinfo/*_db/*_zck
            continue
        if stream_type in kept:
            raise SystemExit(f"{rid}: repomd.xml contains duplicate {stream_type!r} streams")
        kept.append(stream_type)

        loc = data.find(f"{{{_REPOMD_NS}}}location")
        chk = data.find(f"{{{_REPOMD_NS}}}checksum[@type='sha256']")
        size = data.find(f"{{{_REPOMD_NS}}}size")  # compressed size avoids a download-action HEAD probe
        href = loc.get("href") if loc is not None else None
        if href is None or chk is None or chk.text is None or size is None or size.text is None:
            raise SystemExit(f"{rid}: {data.get('type')} record lacks a location, sha256 checksum, or size")
        location_base = None if loc is None else (loc.get("base") or loc.get(f"{{{_XML_NS}}}base"))
        if location_base is not None:
            raise SystemExit(f"{rid}: {stream_type} stream has unsupported location {href!r}")
        href = _relative_href(rid, f"{stream_type} stream", href)
        output = PurePosixPath(href).name
        if output in outputs:
            raise SystemExit(f"{rid}: repomd.xml streams share output basename {output!r}")
        outputs.add(output)
        stream = {
            "out": output,
            "url": base + href,
            "sha256": _sha256(rid, f"{stream_type} stream", chk.text),
            "size": _metadata_int(rid, f"{stream_type} stream", size.text, minimum=1),
        }
        streams.append(stream)
        if data.get("type") == "primary":
            primary = stream

    if missing := [t for t in _BUILD_STREAMS if t not in kept]:
        raise SystemExit(f"{rid}: repomd.xml missing {missing}")
    if primary is None:
        raise SystemExit(f"{rid}: repomd.xml has no primary stream")

    filtered = ET.tostring(root, encoding="unicode", xml_declaration=True)
    packages = _snapshot_packages(rid, primary)
    return {"packages": packages, "repomd": filtered, "streams": streams}


def _write_snapshot(path: Path, fragment: dict) -> None:
    """Atomically replace a snapshot with deterministic, reviewable UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
            newline="\n",
        ) as output:
            temporary = Path(output.name)
            # The package pool is large (~175k entries across the default catalog). Keep
            # one complete entry per line instead of letting indent=2 turn every generated
            # record into four lines; the shape remains ordinary JSON and diffs stay useful.
            output.write('{\n  "packages": {\n')
            packages = sorted(fragment["packages"].items())
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
            json.dump(fragment["repomd"], output)
            output.write(',\n  "streams": ')
            streams = json.dumps(fragment["streams"], indent=2, sort_keys=True)
            output.write(streams.replace("\n", "\n  "))
            output.write("\n}\n")
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="snapshot")
    p.add_argument(
        "--manifest",
        required=True,
        help="the repository's manifest ({id, baseurl} JSON, from its [manifest] sub-target)",
    )
    p.add_argument(
        "--out",
        required=True,
        help="snapshot path to write ({packages, repomd, streams} JSON)",
    )
    args = p.parse_args(argv)

    entry = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    print(f"{entry['id']}: snapshotting repodata…", file=sys.stderr)
    fragment = snapshot_repodata(entry["id"], entry["baseurl"])
    out = Path(args.out)
    _write_snapshot(out, fragment)
    print(f"wrote {out} ({len(fragment['packages'])} packages)", file=sys.stderr)


if __name__ == "__main__":
    main()
