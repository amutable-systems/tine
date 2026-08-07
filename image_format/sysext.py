#!/usr/bin/python3
"""Create a systemd system-extension DDI from a logical image via systemd-repart."""

import hashlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import specs
import util

import finalize
import repart_signing
import rootfs


class Spec(finalize.ImageSpec):
    # Leading lowers forming the base image; only the delta above them is packaged.
    base: int
    identity: str
    seed: str | None
    name: str
    # extension-release fields, in the order the driver writes them.
    release: dict[str, str]
    # Image paths holding the package database, stripped from the DDI.
    pkgdb_paths: list[str]
    signing: repart_signing.KeySpec | None
    out: str


_SEED_NAMESPACE = uuid.UUID("b388a973-3ffa-44aa-90b7-afcc4573ea9f")

# erofs dedup keys off the filesystem UUID, so combined with a stable seed these keep the DDI
# reproducible.
_MKFS_OPTIONS_EROFS = "--quiet -zzstd,level=3 -C524288 -Efragments,ztailpacking,dedupe"


def _derived_seed(identity: str, name: str, release: dict[str, str]) -> uuid.UUID:
    """Derive UUIDs from the logical configuration, keeping repeat builds identical."""
    digest = hashlib.sha256(identity.encode())
    digest.update(b"\0name\0" + name.encode())
    for key, value in release.items():
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
    spec: Spec = specs.parse("sysext", argv)

    release = dict(spec["release"])
    base = spec["base"]
    lowers: list[str | Path] = list(spec["lower"])
    if base < 0 or (base and base >= len(lowers)):
        raise SystemExit("sysext: base must leave at least one delta layer")
    if base:
        # The extension must pin the base identity it was built against, so systemd-sysext
        # refuses to merge it onto anything else.
        with rootfs.rootfs("/buildroot", lowers=lowers) as tree:
            fields = _os_release(tree)
        if "ID" not in fields:
            raise SystemExit("sysext: the base os-release lacks ID")
        strict = {key: fields[key] for key in ("ID", "VERSION_ID") if key in fields and key not in release}
        release = strict | release
        lowers = lowers[base:]

    out = Path(spec["out"])
    with finalize.image(spec, program="sysext", lowers=lowers) as tree:
        # A sysext identifies itself solely through its extension-release; the base os-release
        # must not ride along into the merged /usr.
        (tree / "usr/lib/os-release").unlink(missing_ok=True)
        # The package database is a supply-chain artifact and must not shadow the host's on
        # merge; the image's `[pkgdb]` subtarget captures it separately.
        for relative in spec["pkgdb_paths"]:
            util.remove_path(tree / relative, with_parents=True)
        # --make-ddi copies /usr and /opt; a delta may lack /opt entirely.
        (tree / "opt").mkdir(exist_ok=True)
        release_dir = tree / "usr/lib/extension-release.d"
        release_dir.mkdir(parents=True, exist_ok=True)
        content = "".join(f"{key}={value}\n" for key, value in release.items())
        (release_dir / f"extension-release.{spec['name']}").write_text(content)

        seed = (
            uuid.UUID(spec["seed"])
            if spec["seed"]
            else _derived_seed(spec["identity"], spec["name"], release)
        )
        cmd = [
            "systemd-repart",
            "--make-ddi=sysext",
            f"--copy-source={tree}",
            "--dry-run=no",
            "--offline=yes",
            "--no-pager",
            "--seed",
            str(seed),
        ]
        # The built-in sysext definitions always include the signature partition, and repart
        # refuses to fill it without a key, so an unsigned DDI must drop it instead.
        if spec["signing"]:
            cmd += repart_signing.key_arguments(spec["signing"])
        else:
            cmd.append("--exclude-partitions=root-verity-sig")
        cmd.append(str(out))
        env = os.environ | {"SYSTEMD_REPART_MKFS_OPTIONS_EROFS": _MKFS_OPTIONS_EROFS}
        subprocess.run(cmd, check=True, env=env)

    print(f"sysext: wrote {out.name} (seed={seed})", file=sys.stderr)


if __name__ == "__main__":
    main()
