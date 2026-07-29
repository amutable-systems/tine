#!/usr/bin/python3
"""Create independent partitions or a composed GPT image with systemd-repart."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self, TypedDict

import specs

import finalize

_SEED_NAMESPACE = uuid.UUID("5af2de99-4f9f-4e0b-a04b-bde36b068c4f")


class ImportedPartitionSpec(TypedDict):
    definition: dict[str, Any]
    blocks: str
    metadata: str


class SplitOutputSpec(TypedDict):
    name: str
    blocks: str
    metadata: str


class Spec(finalize.ImageSpec):
    out: str | None
    # Stable target identity the seed derives from, unless one is given outright.
    identity: str
    seed: str | None
    private_key: str | None
    certificate: str | None
    definitions: list[dict[str, Any]]
    # Independent partitions copied into the result, and the new ones written out.
    partitions: list[ImportedPartitionSpec]
    split_outputs: list[SplitOutputSpec]
    root_hash_out: str | None


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
    spec: Spec = specs.parse("repart", argv)

    if not spec["out"] and not spec["split_outputs"]:
        raise SystemExit("repart: specify a disk output, split outputs, or both")
    if not spec["definitions"] and not spec["partitions"]:
        raise SystemExit("repart: specify at least one definition or imported partition")
    raw_definitions = spec["definitions"]
    definitions = [Definition.parse(value) for value in raw_definitions]
    partitions = [
        ImportedPartition(
            Definition.parse(partition["definition"]),
            Path(partition["blocks"]).resolve(),
            Path(partition["metadata"]).resolve(),
        )
        for partition in spec["partitions"]
    ]
    outputs = [
        SplitOutput(
            output["name"],
            Path(output["blocks"]).resolve(),
            Path(output["metadata"]).resolve(),
        )
        for output in spec["split_outputs"]
    ]

    binds: list[tuple[str | Path, str | Path]] = [
        (partition.blocks, f"/run/tine/repart/{index}.raw")
        for index, partition in enumerate(partitions, start=len(definitions))
    ]
    with (
        finalize.image(spec, program="repart", binds=binds) as tree,
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

        seed = (
            uuid.UUID(spec["seed"])
            if spec["seed"]
            else _derived_seed(spec["identity"], raw_definitions, partitions)
        )
        out = Path(spec["out"]).resolve() if spec["out"] else None
        disk = scratch / "image.raw" if outputs else out
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
        if outputs:
            cmd.append("--split=yes")
        if spec["private_key"]:
            cmd += ["--private-key", str(Path(spec["private_key"]).resolve())]
        if spec["certificate"]:
            cmd += ["--certificate", str(Path(spec["certificate"]).resolve())]
        result = subprocess.run([*cmd, str(disk)], check=True, stdout=subprocess.PIPE, text=True)
        rows: list[dict[str, Any]] = json.loads(result.stdout)

        for output in outputs:
            _copy_partition(_partition_row(rows, files[output.name]), output)
        if spec["root_hash_out"]:
            _write_root_hash(rows, Path(spec["root_hash_out"]).resolve())
        if out and outputs:
            shutil.copyfile(disk, out)

    artifacts = []
    if out:
        artifacts.append(out.name)
    if outputs:
        artifacts.append(f"{len(outputs)} partitions")
    mode = " and ".join(artifacts)
    print(f"repart: wrote {mode} (seed={seed})", file=sys.stderr)


if __name__ == "__main__":
    main()
