#!/usr/bin/python3
"""createrepo — generate repodata for a local package repository (Action 0).

Runs *inside* the engine root (which carries python3 + python3-createrepo_c), so
the metadata is produced by the *pinned* createrepo_c, never the host's. Reads a
directory of rpms (a distribution's buildroot-repo package tree), hardlinks them into
the output repo directory, and writes a standard `repodata/` alongside — so the
result is an actual local repository libdnf5 resolves against via a `file://`
baseurl. The repomd revision is pinned to SOURCE_DATE_EPOCH (injected by the
sandbox), so identical inputs yield byte-identical repodata and the repo
content-keys deterministically.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

import createrepo_c as cr

# The three metadata streams every repo carries, paired with their writer class.
_STREAMS = (
    ("primary", cr.PrimaryXmlFile),
    ("filelists", cr.FilelistsXmlFile),
    ("other", cr.OtherXmlFile),
)


def _link_or_copy(src: Path, dst: Path) -> None:
    # Hardlink (cheap, and a real inode — unlike a symlink, which dangles once the
    # tree is bound into a sandbox); fall back to a copy across filesystems.
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def createrepo(entries: list[tuple[str, Path]], out: Path, revision: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    entries = sorted(entries)
    if not entries:
        raise SystemExit("no packages given")

    # libdnf5 resolves each package's location_href relative to the repo baseurl,
    # so the rpms live in the repo dir (under their href) next to repodata/.
    for href, rpm in entries:
        dst = out / href
        dst.parent.mkdir(parents=True, exist_ok=True)
        _link_or_copy(rpm, dst)

    repodata = out / "repodata"
    repodata.mkdir(exist_ok=True)

    paths = {name: str(repodata / f"{name}.xml.gz") for name, _ in _STREAMS}
    writers = {name: cls(paths[name]) for name, cls in _STREAMS}
    for w in writers.values():
        w.set_num_of_pkgs(len(entries))
    for href, rpm in entries:
        pkg = cr.package_from_rpm(str(rpm))
        pkg.location_href = href
        for w in writers.values():
            w.add_pkg(pkg)
    for w in writers.values():
        w.close()

    # repomd.xml ties the records together; pin the revision (not wall-clock) so
    # the repodata is reproducible.
    repomd = cr.Repomd()
    repomd.set_revision(revision)
    for name, _ in _STREAMS:
        rec = cr.RepomdRecord(name, paths[name])
        rec.fill(cr.SHA256)  # ty: ignore[unresolved-attribute]  # SWIG dynamic constant
        rec.rename_file()  # standard <checksum>-<name>.xml.gz layout
        repomd.set_record(rec)
    (repodata / "repomd.xml").write_text(repomd.xml_dump())

    print(f"createrepo: {len(entries)} packages → {out}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="createrepo")
    p.add_argument("--package", action="append", default=[], help="an rpm to publish; repeatable")
    p.add_argument(
        "--packages-dir",
        action="append",
        default=[],
        help="a dir whose *.rpm (excluding .src.rpm) are published under <dir-index>/; repeatable",
    )
    p.add_argument("--out", required=True, help="output repo dir (rpms + repodata/)")
    args = p.parse_args(argv)
    entries = [(Path(p).name, Path(p).resolve()) for p in args.package]
    # Keyed by dir index so a consumer can map a resolved location back to the input dir it
    # came from (buck's dynamic download projects the rpm from there).
    for i, d in enumerate(args.packages_dir):
        entries += [
            (f"{i}/{p.name}", p)
            for p in sorted(Path(d).resolve().glob("*.rpm"))
            if not p.name.endswith(".src.rpm")
        ]
    # Injected by the sandbox via --source-date-epoch; default keeps the tool
    # runnable standalone.
    revision = os.environ.get("SOURCE_DATE_EPOCH", "0")
    createrepo(entries, Path(args.out).resolve(), revision)


if __name__ == "__main__":
    main()
