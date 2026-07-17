#!/usr/bin/python3
"""Create a systemd system-extension DDI from a logical image via systemd-repart."""

import argparse
import hashlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import util

import finalize
import rootfs

_SEED_NAMESPACE = uuid.UUID("b388a973-3ffa-44aa-90b7-afcc4573ea9f")

# erofs dedup keys off the filesystem UUID, so combined with a stable seed these keep the DDI
# reproducible.
_MKFS_OPTIONS_EROFS = "--quiet -zzstd,level=3 -C524288 -Efragments,ztailpacking,dedupe"


def _derived_seed(identity: str, name: str, release: list[tuple[str, str]]) -> uuid.UUID:
    """Derive UUIDs from the logical configuration, keeping repeat builds identical."""
    digest = hashlib.sha256(identity.encode())
    digest.update(b"\0name\0" + name.encode())
    for key, value in release:
        digest.update(b"\0release\0" + key.encode() + b"=" + value.encode())
    return uuid.uuid5(_SEED_NAMESPACE, digest.hexdigest())


def _os_release(tree: Path) -> dict[str, str]:
    """Parse the base image's os-release for strict extension matching."""
    for candidate in ("usr/lib/os-release", "etc/os-release"):
        path = tree / candidate
        if not path.is_file():
            continue
        fields = {}
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if key and sep and not key.startswith("#"):
                fields[key] = value.strip("\"'")
        return fields
    raise SystemExit("sysext: the base image ships no os-release to match against")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="sysext")
    finalize.add_arguments(p)
    p.add_argument(
        "--base",
        type=int,
        default=0,
        help="leading lowers forming the base image; package only the delta above them",
    )
    p.add_argument("--identity", required=True, help="stable target identity used to derive the seed")
    p.add_argument("--seed", help="explicit GPT/partition UUID seed")
    p.add_argument("--name", required=True, help="extension name; deploy the result as <name>.raw")
    p.add_argument(
        "--release",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="extension-release field (repeatable, order preserved)",
    )
    p.add_argument(
        "--pkgdb-path",
        action="append",
        default=[],
        metavar="PATH",
        help="image path holding the package database, to strip from the DDI (repeatable)",
    )
    p.add_argument("--out", required=True, help="output raw DDI")
    args = p.parse_args(argv)

    release: list[tuple[str, str]] = []
    for raw in args.release:
        key, sep, value = raw.partition("=")
        if not key or not sep:
            raise SystemExit(f"sysext: invalid --release field {raw!r}")
        release.append((key, value))

    lowers: list[str | Path] = args.lower
    if args.base < 0 or (args.base and args.base >= len(lowers)):
        p.error("--base must leave at least one delta layer")
    if args.base:
        # The extension must pin the base identity it was built against, so systemd-sysext
        # refuses to merge it onto anything else.
        with rootfs.rootfs("/buildroot", lowers=lowers) as tree:
            fields = _os_release(tree)
        if "ID" not in fields:
            raise SystemExit("sysext: the base os-release lacks ID")
        given = {key for key, _ in release}
        strict = [(key, fields[key]) for key in ("ID", "VERSION_ID") if key in fields and key not in given]
        release = strict + release
        lowers = lowers[args.base :]

    out = Path(args.out).resolve()
    with finalize.image(args, program="sysext", lowers=lowers) as tree:
        # A sysext identifies itself solely through its extension-release; the base os-release
        # must not ride along into the merged /usr.
        (tree / "usr/lib/os-release").unlink(missing_ok=True)
        # The package database is a supply-chain artifact and must not shadow the host's on
        # merge; the image's `[pkgdb]` facet captures it separately.
        for relative in args.pkgdb_path:
            util.remove_path(tree / relative, with_parents=True)
        # --make-ddi copies /usr and /opt; a delta may lack /opt entirely.
        (tree / "opt").mkdir(exist_ok=True)
        release_dir = tree / "usr/lib/extension-release.d"
        release_dir.mkdir(parents=True, exist_ok=True)
        content = "".join(f"{key}={value}\n" for key, value in release)
        (release_dir / f"extension-release.{args.name}").write_text(content)

        seed = uuid.UUID(args.seed) if args.seed else _derived_seed(args.identity, args.name, release)
        cmd = [
            "systemd-repart",
            "--make-ddi=sysext",
            f"--copy-source={tree}",
            # Unsigned for now: erofs data plus verity hash, no signature partition.
            "--exclude-partitions=root-verity-sig",
            "--dry-run=no",
            "--offline=yes",
            "--no-pager",
            "--seed",
            str(seed),
            str(out),
        ]
        env = os.environ | {"SYSTEMD_REPART_MKFS_OPTIONS_EROFS": _MKFS_OPTIONS_EROFS}
        subprocess.run(cmd, check=True, env=env)

    print(f"sysext: wrote {out.name} (seed={seed})", file=sys.stderr)


if __name__ == "__main__":
    main()
