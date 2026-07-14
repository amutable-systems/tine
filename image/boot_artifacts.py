#!/usr/bin/python3
"""Select and extract semantic boot artifacts from a logical image."""

import argparse
import json
import shutil
import struct
import subprocess
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal

import rootfs

SYSTEMD_ANALYZE = "/usr/bin/systemd-analyze"


@dataclass(frozen=True)
class Section:
    offset: int
    size: int


@dataclass(frozen=True)
class Source:
    path: str
    section: str | None = None


@dataclass(frozen=True)
class Selection:
    kernel_release: str
    uki: Source | None
    kernel: Source
    initrd: Source | None


@dataclass(frozen=True)
class UkiCandidate:
    kernel_release: str
    path: Path
    sections: dict[str, Section]


@dataclass(frozen=True)
class KernelCandidate:
    kernel_release: str
    path: Path


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    value = stream.read(size)
    if len(value) != size:
        raise ValueError("truncated PE binary")
    return value


def _pe_sections(path: Path) -> dict[str, Section]:
    try:
        with path.open("rb") as stream:
            if _read_exact(stream, 2) != b"MZ":
                return {}
            stream.seek(0x3C)
            pe_offset = struct.unpack("<I", _read_exact(stream, 4))[0]
            stream.seek(pe_offset)
            if _read_exact(stream, 4) != b"PE\0\0":
                return {}
            coff = _read_exact(stream, 20)
            section_count = struct.unpack_from("<H", coff, 2)[0]
            optional_size = struct.unpack_from("<H", coff, 16)[0]
            stream.seek(optional_size, 1)
            sections = {}
            file_size = path.stat().st_size
            for _ in range(section_count):
                header = _read_exact(stream, 40)
                name = header[:8].partition(b"\0")[0].decode("ascii", "strict")
                virtual_size = struct.unpack_from("<I", header, 8)[0]
                raw_size = struct.unpack_from("<I", header, 16)[0]
                offset = struct.unpack_from("<I", header, 20)[0]
                size = raw_size if virtual_size == 0 else min(virtual_size, raw_size)
                if offset + size > file_size:
                    raise SystemExit(f"boot artifacts: PE section {name!r} exceeds {path}")
                sections[name] = Section(offset=offset, size=size)
            return sections
    except OSError, UnicodeDecodeError, ValueError, struct.error:
        return {}


def _section_bytes(path: Path, section: Section) -> bytes:
    with path.open("rb") as stream:
        stream.seek(section.offset)
        return _read_exact(stream, section.size)


def _section_text(path: Path, section: Section) -> str:
    try:
        return _section_bytes(path, section).rstrip(b"\0\n").decode("utf-8")
    except (UnicodeDecodeError, ValueError) as error:
        raise SystemExit(f"boot artifacts: invalid text section in {path}: {error}") from error


def _image_path(tree: Path, value: str) -> Path:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"boot artifacts: invalid image path {value!r}")
    return tree.joinpath(*path.parts[1:])


def _image_name(tree: Path, path: Path) -> str:
    return "/" + path.relative_to(tree).as_posix()


def _version_compare(left: str, right: str) -> int:
    if left == right:
        return 0
    for relation, result in (("gt", 1), ("lt", -1)):
        proc = subprocess.run(
            [SYSTEMD_ANALYZE, "compare-versions", left, relation, right],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if proc.returncode == 0:
            return result
        if proc.returncode != 1:
            raise SystemExit("boot artifacts: systemd-analyze compare-versions failed")
    return 0


def _uki_compare(left: UkiCandidate, right: UkiCandidate) -> int:
    result = _version_compare(left.kernel_release, right.kernel_release)
    return result if result else (str(left.path) > str(right.path)) - (str(left.path) < str(right.path))


def _kernel_compare(left: KernelCandidate, right: KernelCandidate) -> int:
    result = _version_compare(left.kernel_release, right.kernel_release)
    return result if result else (str(left.path) > str(right.path)) - (str(left.path) < str(right.path))


def _ukis(tree: Path) -> list[UkiCandidate]:
    candidates = []
    for directory in (tree / "boot/EFI/Linux", tree / "efi/EFI/Linux"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.efi")):
            sections = _pe_sections(path)
            if ".linux" not in sections or ".uname" not in sections:
                continue
            kernel_release = _section_text(path, sections[".uname"])
            if kernel_release:
                candidates.append(UkiCandidate(kernel_release, path, sections))
    return candidates


def _kernels(tree: Path) -> list[KernelCandidate]:
    candidates = []
    modules = tree / "usr/lib/modules"
    if modules.is_dir():
        for directory in sorted(modules.iterdir()):
            kernel = directory / "vmlinuz"
            if directory.is_dir() and kernel.is_file():
                candidates.append(KernelCandidate(directory.name, kernel))
    boot = tree / "boot"
    if boot.is_dir():
        for kernel in sorted(boot.glob("vmlinuz-*")):
            if kernel.is_file():
                candidates.append(KernelCandidate(kernel.name.removeprefix("vmlinuz-"), kernel))
    return candidates


def _matching_initrd(tree: Path, kernel_release: str) -> Path | None:
    candidates = (
        tree / f"boot/initramfs-{kernel_release}.img",
        tree / f"boot/initrd.img-{kernel_release}",
        tree / f"boot/initrd-{kernel_release}.img",
        tree / f"boot/initrd-{kernel_release}",
        tree / "usr/lib/modules" / kernel_release / "initrd",
    )
    return next((path for path in candidates if path.is_file()), None)


def _select(tree: Path) -> Selection:
    ukis = _ukis(tree)
    if ukis:
        uki = max(ukis, key=cmp_to_key(_uki_compare))
        initrd = Source(_image_name(tree, uki.path), ".initrd") if ".initrd" in uki.sections else None
        if initrd is None and (standalone := _matching_initrd(tree, uki.kernel_release)) is not None:
            initrd = Source(_image_name(tree, standalone))
        if initrd is None:
            raise SystemExit(f"boot artifacts: selected kernel {uki.kernel_release} has no matching initrd")
        source = Source(_image_name(tree, uki.path))
        return Selection(
            kernel_release=uki.kernel_release,
            uki=source,
            kernel=Source(source.path, ".linux"),
            initrd=initrd,
        )

    kernels = _kernels(tree)
    if not kernels:
        raise SystemExit("boot artifacts: image contains no UKI or standalone kernel")
    kernel = max(kernels, key=cmp_to_key(_kernel_compare))
    initrd = _matching_initrd(tree, kernel.kernel_release)
    if initrd is None:
        raise SystemExit(f"boot artifacts: selected kernel {kernel.kernel_release} has no matching initrd")
    return Selection(
        kernel_release=kernel.kernel_release,
        uki=None,
        kernel=Source(_image_name(tree, kernel.path)),
        initrd=Source(_image_name(tree, initrd)),
    )


def _source_json(source: Source | None) -> dict[str, str | None] | None:
    return {"path": source.path, "section": source.section} if source is not None else None


def _write_selection(selection: Selection, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "kernel_release": selection.kernel_release,
                "uki": _source_json(selection.uki),
                "kernel": _source_json(selection.kernel),
                "initrd": _source_json(selection.initrd),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _parse_source(value: object, name: str, *, optional: bool) -> Source | None:
    if value is None and optional:
        return None
    if not isinstance(value, dict) or set(value) != {"path", "section"}:
        raise SystemExit(f"boot artifacts: invalid {name} selection")
    path = value.get("path")
    section = value.get("section")
    if not isinstance(path, str) or (section is not None and not isinstance(section, str)):
        raise SystemExit(f"boot artifacts: invalid {name} selection")
    return Source(path, section)


def _read_selection(path: Path) -> Selection:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"kernel_release", "uki", "kernel", "initrd"}:
        raise SystemExit("boot artifacts: invalid selection manifest")
    kernel_release = value.get("kernel_release")
    if not isinstance(kernel_release, str):
        raise SystemExit("boot artifacts: invalid kernel release")
    kernel = _parse_source(value.get("kernel"), "kernel", optional=False)
    assert kernel is not None
    return Selection(
        kernel_release=kernel_release,
        uki=_parse_source(value.get("uki"), "UKI", optional=True),
        kernel=kernel,
        initrd=_parse_source(value.get("initrd"), "initrd", optional=True),
    )


def _copy_section(source: Path, section: Section, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, out.open("wb") as dst:
        src.seek(section.offset)
        remaining = section.size
        while remaining:
            chunk = src.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise SystemExit(f"boot artifacts: truncated section in {source}")
            dst.write(chunk)
            remaining -= len(chunk)


def _extract(tree: Path, selection: Selection, kind: Literal["uki", "kernel", "initrd"], out: Path) -> None:
    source = {"uki": selection.uki, "kernel": selection.kernel, "initrd": selection.initrd}[kind]
    if source is None:
        raise SystemExit(
            f"boot artifacts: selected kernel {selection.kernel_release} has no matching {kind}"
        )
    path = _image_path(tree, source.path)
    if source.section is None:
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, out)
        return
    sections = _pe_sections(path)
    section = sections.get(source.section)
    if section is None:
        raise SystemExit(f"boot artifacts: {path} has no {source.section} section")
    _copy_section(path, section, out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="boot-artifacts")
    parser.add_argument("action", choices=("select", "extract"))
    parser.add_argument("--lower", action="append", default=[], help="image delta (bottom..top)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--kind", choices=("uki", "kernel", "initrd"))
    args = parser.parse_args(argv)

    if args.action == "select" and (args.manifest is not None or args.kind is not None):
        parser.error("select does not accept --manifest or --kind")
    if args.action == "extract" and (args.manifest is None or args.kind is None):
        parser.error("extract requires --manifest and --kind")

    with rootfs.rootfs("/buildroot", lowers=args.lower) as tree:
        out = Path(args.out).resolve()
        if args.action == "select":
            _write_selection(_select(tree), out)
        else:
            selection = _read_selection(Path(args.manifest).resolve())
            _extract(tree, selection, args.kind, out)


if __name__ == "__main__":
    main()
