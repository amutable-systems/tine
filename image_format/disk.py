#!/usr/bin/python3
"""Create independent partitions or a composed GPT image with systemd-repart."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Self, TypedDict

import specs
import util

import finalize
import manifest
import repart

_SEED_NAMESPACE = uuid.UUID("5af2de99-4f9f-4e0b-a04b-bde36b068c4f")


class WrittenPartitionSpec(TypedDict):
    definition: dict[str, Any]
    # Where to write it as a standalone artifact and the metadata describing it, for an invocation
    # that splits its partitions back out; absent for one that only composes them into a disk.
    blocks: str | None
    metadata: str | None
    # Where to list what it carries; absent for a partition that copies nothing, so there is
    # nothing to list: a verity partition holds a hash tree, and a filesystem created empty is
    # populated by the system that grows it rather than by this build.
    manifest: str | None


class ImportedPartitionSpec(TypedDict):
    definition: dict[str, Any]
    # Where it already is, written by the invocation that filled it, and the metadata describing it.
    blocks: str
    metadata: str
    # What it carries, listed by that same invocation; absent for the same reason.
    manifest: str | None


class Spec(finalize.ImageSpec):
    out: str | None
    # What the artifacts of this invocation are named after, extension aside.
    basename: str
    # Stable target identity the seed derives from, unless one is given outright.
    identity: str
    output_size: str | None
    seed: str | None
    signing: repart.KeySpec | None
    # The partitions this invocation writes, and the independent ones copied into the result.
    definitions: list[WrittenPartitionSpec]
    partitions: list[ImportedPartitionSpec]
    root_hash_out: str | None
    # Image paths holding the package database, stripped from the partitions.
    pkgdb_paths: list[str]
    # Where to list what the composed disk carries, its partitions taken together.
    manifest: str | None
    # Filesystem -> the options mkfs is given for it.
    mkfs_options: dict[str, list[str]]


@dataclass(frozen=True)
class Definition:
    name: str
    type: str
    label: str | None
    filesystem: str | None
    copy_files: tuple[str, ...]
    size_min: str | int | None
    size_max: str | int | None
    minimize: str | None
    compression: str | None
    verity: str | None
    verity_match_key: str | None

    @classmethod
    def parse(cls, value: dict[str, Any]) -> Self:
        return cls(
            name=value["name"],
            type=value["type"],
            label=value["label"],
            filesystem=value["filesystem"],
            copy_files=tuple(value["copy_files"]),
            size_min=value["size_min"],
            size_max=value["size_max"],
            minimize=value["minimize"],
            compression=value["compression"],
            verity=value["verity"],
            verity_match_key=value["verity_match_key"],
        )

    @property
    def sources(self) -> tuple[str, ...]:
        """The image paths this partition is populated from, whatever it is they are copied to."""
        return tuple(copy.split(":", 1)[0] for copy in self.copy_files)

    def render(self, *, split: bool) -> str:
        lines = ["[Partition]", f"Type={self.type}"]
        _setting(lines, "Label", self.label)
        _setting(lines, "Format", self.filesystem)
        _setting(lines, "SizeMinBytes", self.size_min)
        _setting(lines, "SizeMaxBytes", self.size_max)
        _setting(lines, "Minimize", self.minimize)
        _setting(lines, "Compression", self.compression)
        _setting(lines, "Verity", self.verity)
        _setting(lines, "VerityMatchKey", self.verity_match_key)
        for copy in self.copy_files:
            lines.append(f"CopyFiles={copy}")
        if split:
            # repart resolves this against the name of the image it writes, so a split artifact
            # leaves the run already named the way it is published: %t is the partition type and
            # %U the UUID it generated, which systemd-sysupdate reads back out of the name as @u
            # to give the partition it writes that same UUID.
            lines.append("SplitName=%t.%U")
        return "\n".join(lines) + "\n"

    def render_import(self, blocks: Path, metadata: dict[str, Any]) -> str:
        """Preserve the partition's identity while replacing population with CopyBlocks."""
        lines = [
            "[Partition]",
            f"Type={metadata['type']}",
            f"UUID={metadata['uuid']}",
        ]
        _setting(lines, "Label", metadata.get("label"))
        _setting(lines, "SizeMinBytes", self.size_min)
        _setting(lines, "SizeMaxBytes", self.size_max)
        _setting(lines, "Verity", self.verity)
        _setting(lines, "VerityMatchKey", self.verity_match_key)
        lines.append(f"CopyBlocks={blocks}")

        # An imported partition already exists as a standalone artifact, so splitting it back out
        # would write a second copy of it and discard that. SplitName= otherwise defaults to %t.
        lines.append("SplitName=-")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class WrittenPartition:
    definition: Definition
    blocks: Path | None
    metadata: Path | None
    manifest: Path | None


@dataclass(frozen=True)
class ImportedPartition:
    definition: Definition
    blocks: Path
    metadata: Path
    manifest: Path | None


def _setting(lines: list[str], name: str, value: object | None) -> None:
    if value is not None:
        lines.append(f"{name}={value}")


def _derived_seed(
    identity: str,
    definitions: list[dict[str, Any]],
    partitions: list[ImportedPartition],
) -> uuid.UUID:
    """Derive UUIDs from logical configuration, not action-local CopyBlocks paths.

    A definition is re-encoded exactly as the rule spelled it, key order included (see
    disk.bzl:partition): any other encoding re-identifies every partition of every
    unchanged image.
    """
    digest = hashlib.sha256(identity.encode())
    for definition in definitions:
        digest.update(b"\0definition\0" + json.dumps(definition, separators=(",", ":")).encode())
    for partition in partitions:
        digest.update(b"\0partition\0" + partition.definition.name.encode() + b"\0")
        digest.update(partition.metadata.read_bytes())
    return uuid.uuid5(_SEED_NAMESPACE, digest.hexdigest())


def _write_definitions(
    directory: Path,
    written: list[WrittenPartition],
    partitions: list[ImportedPartition],
    *,
    split: bool,
) -> dict[str, str]:
    files = {}
    index = 0
    for partition in written:
        filename = f"{index:04}-{partition.definition.name}.conf"
        (directory / filename).write_text(partition.definition.render(split=split))
        files[partition.definition.name] = filename
        index += 1
    for partition in partitions:
        metadata: dict[str, Any] = json.loads(partition.metadata.read_text())
        filename = f"{index:04}-{partition.definition.name}.conf"
        copy_path = Path(f"/run/tine/repart/{index}.raw")
        (directory / filename).write_text(partition.definition.render_import(copy_path, metadata))
        index += 1
    return files


def _partition_row(rows: list[dict[str, Any]], filename: str) -> dict[str, Any]:
    matches = [row for row in rows if Path(row.get("file", "")).name == filename]
    if len(matches) != 1:
        raise SystemExit(f"repart: expected one result for {filename}, found {len(matches)}")
    return matches[0]


def _copy_partition(row: dict[str, Any], name: str, blocks: Path, metadata: Path) -> None:
    source_value = row.get("split_path")
    if not source_value or source_value == "-":
        raise SystemExit(f"repart: no split artifact was produced for {name}")
    source = Path(source_value)
    blocks.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, blocks)

    # systemd strips padding from signature split files. CopyBlocks requires a
    # sector-aligned artifact, so restore the partition's declared raw size.
    raw_size = int(row["raw_size"])
    if blocks.stat().st_size > raw_size:
        raise SystemExit(f"repart: split artifact for {name} exceeds its partition")
    with blocks.open("r+b") as f:
        f.truncate(raw_size)

    described = {
        "label": row.get("label"),
        "published": source.name,
        "raw_size": raw_size,
        "type": row["type"],
        "uuid": row["uuid"],
    }
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(json.dumps(described, sort_keys=True, separators=(",", ":")) + "\n")


def _write_root_hash(rows: list[dict[str, Any]], output: Path) -> None:
    hashes = {value for row in rows if (value := row.get("roothash")) not in (None, "TBD")}
    if len(hashes) != 1:
        raise SystemExit(f"repart: expected one generated verity root hash, found {len(hashes)}")
    output.write_text(hashes.pop() + "\n")


def _grow(disk: Path, size: str) -> None:
    """Extend a composed disk to its requested size, leaving the added room a hole.

    repart sizes the disk to what its partitions need; the extra room is not something any of them
    should claim, so the file is enlarged after the fact instead. As with vmspawn's --grow-image,
    the room sits past the GPT backup header until something rewrites the table.
    """
    suffixes = "KMGTPE"  # systemd reads them base-1024
    target = (
        int(Decimal(size[:-1]) * 1024 ** (suffixes.index(size[-1]) + 1))
        if size[-1] in suffixes
        else int(size)
    )
    composed = disk.stat().st_size
    if composed > target:
        raise SystemExit(f"repart: the partitions need {composed} bytes, more than the requested {size}")
    with disk.open("r+b") as f:
        f.truncate(target)


def main(argv: list[str] | None = None) -> None:
    spec: Spec = specs.parse("repart", argv)

    if not spec["definitions"] and not spec["partitions"]:
        raise SystemExit("repart: specify at least one definition or imported partition")
    raw_definitions = [entry["definition"] for entry in spec["definitions"]]
    written = [
        WrittenPartition(
            Definition.parse(entry["definition"]),
            Path(entry["blocks"]) if entry["blocks"] else None,
            Path(entry["metadata"]) if entry["metadata"] else None,
            Path(entry["manifest"]) if entry["manifest"] else None,
        )
        for entry in spec["definitions"]
    ]
    partitions = [
        ImportedPartition(
            Definition.parse(partition["definition"]),
            Path(partition["blocks"]),
            Path(partition["metadata"]),
            Path(partition["manifest"]) if partition["manifest"] else None,
        )
        for partition in spec["partitions"]
    ]
    # A partition named as its own artifact is one repart splits back out, which is a property of
    # the invocation rather than of any one partition: it splits all of them or none.
    splits = [
        (partition.definition.name, partition.blocks, partition.metadata)
        for partition in written
        if partition.blocks is not None and partition.metadata is not None
    ]
    if not spec["out"] and not splits:
        raise SystemExit("repart: specify a disk output, split outputs, or both")

    binds: list[tuple[str | Path, str | Path]] = [
        (partition.blocks, f"/run/tine/repart/{index}.raw")
        for index, partition in enumerate(partitions, start=len(written))
    ]
    with (
        finalize.image(spec, program="repart", binds=binds) as tree,
        tempfile.TemporaryDirectory(prefix="repart.") as scratch_dir,
    ):
        # The package database is a supply-chain artifact that the image's `[pkgdb]` and `[sbom]`
        # subtargets capture from the tree, so partitions nothing resolves packages in can drop it.
        for relative in spec["pkgdb_paths"]:
            util.remove_path(tree / relative, with_parents=True)

        # What each partition ends up carrying, listed from the tree repart is about to copy. A
        # definition takes the paths it names and nothing else, so a disk whose definitions are a
        # /usr partition and an ESP has no listing of /var, which lands nowhere.
        epoch = int(os.environ["SOURCE_DATE_EPOCH"])
        listings = []
        for partition in written:
            if partition.manifest is not None:
                manifest.write(tree, partition.manifest, epoch, roots=partition.definition.sources)
                listings.append(partition.manifest)

        # An imported partition was listed by the invocation that filled it, and this one holds
        # what it copies itself, so between them they list the whole disk. Imported first: a
        # partition this invocation mounts over a directory another one holds is the one on top.
        if spec["manifest"]:
            imported = [it.manifest for it in partitions if it.manifest is not None]
            manifest.merge(imported + listings, Path(spec["manifest"]))

        scratch = Path(scratch_dir)
        repart_definitions = scratch / "repart.d"
        repart_definitions.mkdir()
        files = _write_definitions(
            repart_definitions,
            written,
            partitions,
            split=bool(splits),
        )

        seed = (
            uuid.UUID(spec["seed"])
            if spec["seed"]
            else _derived_seed(spec["identity"], raw_definitions, partitions)
        )
        out = Path(spec["out"]) if spec["out"] else None
        # A split artifact is named after the image repart writes, so the scratch file carries the
        # published basename even when only the partitions leave this invocation.
        disk = scratch / f"{spec['basename']}.raw" if splits else out
        assert disk is not None  # one of a disk output and split outputs is required
        cmd = [
            "systemd-repart",
            "--empty=create",
            "--size=auto",
            "--dry-run=no",
            "--json=pretty",
            "--no-pager",
            f"--root={tree}",
            "--offline=yes",
            "--seed",
            str(seed),
            "--definitions",
            str(repart_definitions),
        ]
        if splits:
            cmd.append("--split=yes")
        cmd += repart.key_arguments(spec["signing"])
        # repart reads one variable per filesystem it formats and splits each on whitespace.
        env = os.environ | {
            f"SYSTEMD_REPART_MKFS_OPTIONS_{filesystem.upper()}": " ".join(options)
            for filesystem, options in spec["mkfs_options"].items()
        }
        result = subprocess.run([*cmd, str(disk)], check=True, env=env, stdout=subprocess.PIPE, text=True)
        rows: list[dict[str, Any]] = json.loads(result.stdout)

        for name, blocks, metadata in splits:
            _copy_partition(_partition_row(rows, files[name]), name, blocks, metadata)
        if spec["root_hash_out"]:
            _write_root_hash(rows, Path(spec["root_hash_out"]))
        if out and splits:
            shutil.copyfile(disk, out)
        if out and spec["output_size"]:
            _grow(out, spec["output_size"])

    artifacts = []
    if out:
        artifacts.append(out.name)
    if splits:
        artifacts.append(f"{len(splits)} partitions")
    mode = " and ".join(artifacts)
    print(f"repart: wrote {mode} (seed={seed})", file=sys.stderr)


if __name__ == "__main__":
    main()
