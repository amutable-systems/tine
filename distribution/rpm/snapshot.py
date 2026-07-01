"""snapshot — pin one repository's build-time repodata into its lock fragment.

Run on refresh (via the repository target's `[snapshot]` sub-target, driven by the host
orchestrator //distribution:buckify), NOT during builds. A repository is a first-class
catalog citizen shared across distributions, so it pins independently of any of them —
and pinning is pure fetch-and-filter (urllib + ElementTree, no libdnf5), so unlike the
engine-closure resolve this runs on the host, needing no engine.

Reads the repository's manifest ({id, baseurl}, materialized from the catalog BUCK),
fetches `repodata/repomd.xml`, drops every <data> record except the build streams, and
writes the fragment: {repomd: <filtered xml>, streams: [{out, url, sha256, size}]}. Buck
reconstructs the pinned `repodata/` tree from it (the rpm_remote_repository rule downloads
the streams); the resolved rpms are fetched lazily at build time from the manifest's
baseurl. Re-running against an unchanged upstream reproduces the fragment byte-for-byte.
"""

import argparse
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# repomd.xml's namespace (the <data>/<location>/<checksum> elements live here).
_REPOMD_NS = "http://linux.duke.edu/metadata/repo"
# The repodata streams the build-time solve needs: primary (provides/requires) + filelists
# (file-path provides, for file-dep BuildRequires), plus group (comps) when the repo publishes
# it — plan expands `@group` installs (the buildroot base) against it at build time.
# other/updateinfo and the db/zck/xz re-encodings aren't needed and are dropped so libdnf5
# never tries to fetch them.
_BUILD_STREAMS = ("primary", "filelists")
_OPTIONAL_STREAMS = ("group",)


def snapshot_repodata(rid: str, baseurl: str) -> dict:
    """Pin one repo's build-time repodata; see the module docstring for the shape."""
    base = baseurl.rstrip("/") + "/"
    with urllib.request.urlopen(base + "repodata/repomd.xml") as f:
        repomd = f.read()

    ET.register_namespace("", _REPOMD_NS)  # keep the default (unprefixed) namespace on output
    root = ET.fromstring(repomd)

    kept = []
    streams = []
    for data in list(root.findall(f"{{{_REPOMD_NS}}}data")):
        if data.get("type") not in _BUILD_STREAMS + _OPTIONAL_STREAMS:
            root.remove(data)  # drop other/updateinfo/*_db/*_zck
            continue
        kept.append(data.get("type"))

        loc = data.find(f"{{{_REPOMD_NS}}}location")
        chk = data.find(f"{{{_REPOMD_NS}}}checksum[@type='sha256']")
        size = data.find(f"{{{_REPOMD_NS}}}size")  # compressed size — lets http_file skip the HEAD probe
        href = loc.get("href") if loc is not None else None
        if href is None or chk is None or chk.text is None or size is None or size.text is None:
            raise SystemExit(f"{rid}: {data.get('type')} record lacks a location, sha256 checksum, or size")
        streams.append(
            {
                "out": href.rsplit("/", 1)[-1],
                "url": base + href,
                "sha256": chk.text,
                "size": int(size.text),
            }
        )

    if missing := [t for t in _BUILD_STREAMS if t not in kept]:
        raise SystemExit(f"{rid}: repomd.xml missing {missing}")

    filtered = ET.tostring(root, encoding="unicode", xml_declaration=True)
    return {"repomd": filtered, "streams": streams}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="snapshot")
    p.add_argument(
        "--manifest",
        required=True,
        help="the repository's manifest ({id, baseurl} JSON, from its [manifest] sub-target)",
    )
    p.add_argument("--out", required=True, help="fragment path to write ({repomd, streams} JSON)")
    args = p.parse_args(argv)

    entry = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    print(f"{entry['id']}: snapshotting repodata…", file=sys.stderr)
    fragment = snapshot_repodata(entry["id"], entry["baseurl"])
    # Explicit encoding/newline so the fragment is byte-identical regardless of host
    # locale or platform newline conventions.
    out = Path(args.out)
    out.write_text(json.dumps(fragment, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
