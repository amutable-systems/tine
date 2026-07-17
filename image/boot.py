#!/usr/bin/python3
"""Select boot artifacts from a logical image."""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import artifacts
import finalize
from mkosi.versioncomp import GenericVersion


@dataclass(frozen=True)
class Source:
    path: str
    section: str | None = None


@dataclass(frozen=True)
class Selection:
    kernel_release: str
    uki: Source | None
    kernel: Source
    initrd: Source


@dataclass(frozen=True)
class UkiCandidate:
    kernel_release: str
    path: Path
    sections: dict[str, artifacts.Section]


@dataclass(frozen=True)
class KernelCandidate:
    kernel_release: str
    path: Path


def _image_name(tree: Path, path: Path) -> str:
    return "/" + path.relative_to(tree).as_posix()


def _candidate_key(candidate: UkiCandidate | KernelCandidate) -> tuple[GenericVersion, str]:
    return GenericVersion(candidate.kernel_release), str(candidate.path)


def _ukis(tree: Path) -> list[UkiCandidate]:
    candidates = []
    for directory in (tree / "boot/EFI/Linux", tree / "efi/EFI/Linux"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.efi")):
            sections = artifacts.pe_sections(path)
            if ".linux" not in sections or ".uname" not in sections:
                continue
            kernel_release = artifacts.section_text(path, sections[".uname"])
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
        uki = max(ukis, key=_candidate_key)
        initrd = Source(_image_name(tree, uki.path), ".initrd") if ".initrd" in uki.sections else None
        if initrd is None and (standalone := _matching_initrd(tree, uki.kernel_release)) is not None:
            initrd = Source(_image_name(tree, standalone))
        if initrd is None:
            raise SystemExit(f"boot: selected kernel {uki.kernel_release} has no matching initrd")
        source = Source(_image_name(tree, uki.path))
        return Selection(
            kernel_release=uki.kernel_release,
            uki=source,
            kernel=Source(source.path, ".linux"),
            initrd=initrd,
        )

    kernels = _kernels(tree)
    if not kernels:
        raise SystemExit("boot: image contains no UKI or standalone kernel")
    kernel = max(kernels, key=_candidate_key)
    initrd = _matching_initrd(tree, kernel.kernel_release)
    if initrd is None:
        raise SystemExit(f"boot: selected kernel {kernel.kernel_release} has no matching initrd")
    return Selection(
        kernel_release=kernel.kernel_release,
        uki=None,
        kernel=Source(_image_name(tree, kernel.path)),
        initrd=Source(_image_name(tree, initrd)),
    )


def _source_json(source: Source) -> dict[str, str | None]:
    return {"path": source.path, "section": source.section}


def _write_selection(selection: Selection, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "kernel_release": selection.kernel_release,
                "uki": _source_json(selection.uki) if selection.uki is not None else None,
                "kernel": _source_json(selection.kernel),
                "initrd": _source_json(selection.initrd),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="boot")
    finalize.add_arguments(parser)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    with finalize.image(args, program="boot") as tree:
        _write_selection(_select(tree), Path(args.out).resolve())


if __name__ == "__main__":
    main()
