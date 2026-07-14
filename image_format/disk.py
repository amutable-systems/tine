#!/usr/bin/python3
"""Create independent partitions or a composed GPT image with systemd-repart."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import finalize

import rootfs

_SEED_NAMESPACE = uuid.UUID("5af2de99-4f9f-4e0b-a04b-bde36b068c4f")


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
    def parse(cls, raw: str) -> Self:
        value: dict[str, Any] = json.loads(raw)
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
            lines.append(f"SplitName={self.name}")
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
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ImportedPartition:
    definition: Definition
    blocks: Path
    metadata: Path


@dataclass(frozen=True)
class SplitOutput:
    name: str
    blocks: Path
    metadata: Path


def _setting(lines: list[str], name: str, value: object | None) -> None:
    if value is not None:
        lines.append(f"{name}={value}")


def _derived_seed(
    identity: str,
    definitions: list[str],
    partitions: list[ImportedPartition],
) -> uuid.UUID:
    """Derive UUIDs from logical configuration, not action-local CopyBlocks paths."""
    digest = hashlib.sha256(identity.encode())
    for definition in definitions:
        digest.update(b"\0definition\0" + definition.encode())
    for partition in partitions:
        digest.update(b"\0partition\0" + partition.definition.name.encode() + b"\0")
        digest.update(partition.metadata.read_bytes())
    return uuid.uuid5(_SEED_NAMESPACE, digest.hexdigest())


def _write_definitions(
    directory: Path,
    definitions: list[Definition],
    partitions: list[ImportedPartition],
    *,
    split: bool,
) -> dict[str, str]:
    files = {}
    index = 0
    for definition in definitions:
        filename = f"{index:04}-{definition.name}.conf"
        (directory / filename).write_text(definition.render(split=split))
        files[definition.name] = filename
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


def _copy_partition(row: dict[str, Any], output: SplitOutput) -> None:
    source_value = row.get("split_path")
    if not source_value or source_value == "-":
        raise SystemExit(f"repart: no split artifact was produced for {output.name}")
    source = Path(source_value)
    output.blocks.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output.blocks)

    # systemd strips padding from signature split files. CopyBlocks requires a
    # sector-aligned artifact, so restore the partition's declared raw size.
    raw_size = int(row["raw_size"])
    if output.blocks.stat().st_size > raw_size:
        raise SystemExit(f"repart: split artifact for {output.name} exceeds its partition")
    with output.blocks.open("r+b") as f:
        f.truncate(raw_size)

    metadata = {
        "label": row.get("label"),
        "raw_size": raw_size,
        "type": row["type"],
        "uuid": row["uuid"],
    }
    output.metadata.parent.mkdir(parents=True, exist_ok=True)
    output.metadata.write_text(json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n")


def _write_root_hash(rows: list[dict[str, Any]], output: Path) -> None:
    hashes = {value for row in rows if (value := row.get("roothash")) not in (None, "TBD")}
    if len(hashes) != 1:
        raise SystemExit(f"repart: expected one generated verity root hash, found {len(hashes)}")
    output.write_text(hashes.pop() + "\n")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="repart")
    p.add_argument("--lower", action="append", default=[], help="the layer stack (bottom..top) to merge")
    p.add_argument("--out", help="output raw disk image")
    p.add_argument("--identity", required=True, help="stable target identity used to derive the seed")
    p.add_argument("--seed", help="explicit GPT/partition UUID seed")
    p.add_argument("--private-key", help="PEM private key for verity signing")
    p.add_argument("--certificate", help="PEM certificate for verity signing")
    p.add_argument(
        "--tmpfiles", action="append", default=[], help="authored tmpfiles.d snippet (repeatable)"
    )
    p.add_argument("--definition", action="append", default=[], help="serialized partition definition")
    p.add_argument(
        "--partition",
        action="append",
        nargs=3,
        default=[],
        metavar=("DEFINITION", "BLOCKS", "METADATA"),
        help="an independent partition to copy into the result",
    )
    p.add_argument(
        "--split-output",
        action="append",
        nargs=3,
        default=[],
        metavar=("NAME", "BLOCKS", "METADATA"),
        help="declared output paths for one new split partition",
    )
    p.add_argument("--root-hash-out", help="write the generated verity root hash here")
    args = p.parse_args(argv)

    if not args.out and not args.split_output:
        p.error("specify --out, at least one --split-output, or both")
    if not args.definition and not args.partition:
        p.error("specify at least one definition or imported partition")
    raw_definitions: list[str] = args.definition
    definitions = [Definition.parse(raw) for raw in raw_definitions]
    partitions = [
        ImportedPartition(Definition.parse(raw), Path(blocks).resolve(), Path(metadata).resolve())
        for raw, blocks, metadata in args.partition
    ]
    outputs = [
        SplitOutput(name, Path(blocks).resolve(), Path(metadata).resolve())
        for name, blocks, metadata in args.split_output
    ]

    binds: list[tuple[str | Path, str | Path]] = [
        (partition.blocks, f"/run/tine/repart/{index}.raw")
        for index, partition in enumerate(partitions, start=len(definitions))
    ]
    with (
        rootfs.rootfs("/buildroot", lowers=args.lower, binds=binds) as tree,
        tempfile.TemporaryDirectory(prefix="repart.") as scratch_dir,
    ):
        scratch = Path(scratch_dir)
        repart_definitions = scratch / "repart.d"
        repart_definitions.mkdir()
        files = _write_definitions(
            repart_definitions,
            definitions,
            partitions,
            split=bool(outputs),
        )

        finalize.apply_tmpfiles(tree, args.tmpfiles, program="repart")
        seed = (
            uuid.UUID(args.seed) if args.seed else _derived_seed(args.identity, raw_definitions, partitions)
        )
        disk = scratch / "image.raw" if outputs else Path(args.out).resolve()
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
        if outputs:
            cmd.append("--split=yes")
        if args.private_key:
            cmd += ["--private-key", str(Path(args.private_key).resolve())]
        if args.certificate:
            cmd += ["--certificate", str(Path(args.certificate).resolve())]
        result = subprocess.run([*cmd, str(disk)], check=True, stdout=subprocess.PIPE, text=True)
        rows: list[dict[str, Any]] = json.loads(result.stdout)

        for output in outputs:
            _copy_partition(_partition_row(rows, files[output.name]), output)
        if args.root_hash_out:
            _write_root_hash(rows, Path(args.root_hash_out).resolve())
        if args.out and outputs:
            shutil.copyfile(disk, Path(args.out).resolve())

    artifacts = []
    if args.out:
        artifacts.append(Path(args.out).name)
    if outputs:
        artifacts.append(f"{len(outputs)} partitions")
    mode = " and ".join(artifacts)
    print(f"repart: wrote {mode} (seed={seed})", file=sys.stderr)


if __name__ == "__main__":
    main()
