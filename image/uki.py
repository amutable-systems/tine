#!/usr/bin/python3
"""Build unified kernel images for every kernel in a logical filesystem image.

Engine tools operate on the mounted image without chrooting. A per-kernel modules cpio
extends the supplied base initrds.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import cpio
import rootfs

# Ukify lives outside PATH in the engine.
UKIFY = "/usr/lib/systemd/ukify"


_ARCH = {
    "x86_64": ("x64", "linuxx64.efi.stub"),
}


def _kvers(tree: Path) -> list[str]:
    modules = tree / "usr/lib/modules"
    kvers = sorted(p.name for p in modules.iterdir() if (p / "vmlinuz").exists()) if modules.is_dir() else []
    if not kvers:
        raise SystemExit("uki: found no kernels under /usr/lib/modules")
    return kvers


def _cmdline(arguments: list[str], root_hash: Path | None, kind: str | None) -> str:
    if root_hash is None:
        return " ".join(arguments)
    assert kind is not None
    parameter = f"{kind}hash"
    if any(word.split("=", 1)[0] == parameter for argument in arguments for word in argument.split()):
        raise SystemExit(f"uki: {parameter}= is both explicit and generated")
    digest = root_hash.read_text().strip()
    if not digest or any(character not in "0123456789abcdefABCDEF" for character in digest):
        raise SystemExit("uki: invalid verity root hash")
    return " ".join([*arguments, f"{parameter}={digest.lower()}"])


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="uki")
    p.add_argument("--lower", action="append", default=[], help="the image stack (bottom..top) to merge")
    p.add_argument("--out", required=True, help="the output unified kernel image directory")
    p.add_argument(
        "--initrd", action="append", default=[], help="base initrd cpio, in load order (repeatable)"
    )
    p.add_argument(
        "--cmdline",
        action="append",
        default=[],
        metavar="ARGUMENT",
        help="kernel command-line argument embedded in the UKI (repeatable)",
    )
    p.add_argument(
        "--profile",
        action="append",
        default=[],
        metavar="PROFILE",
        help="alternative boot profile as JSON with id, title, and cmdline fields (repeatable)",
    )
    p.add_argument("--root-hash", help="file containing a generated verity root hash")
    p.add_argument("--root-hash-kind", choices=("root", "usr"))
    p.add_argument("--arch", required=True, choices=tuple(_ARCH))
    p.add_argument("--entry", required=True, help="filename prefix for generated UKIs")
    args = p.parse_args(argv)

    if bool(args.root_hash) != bool(args.root_hash_kind):
        p.error("--root-hash and --root-hash-kind must be specified together")
    if not args.entry or Path(args.entry).name != args.entry or args.entry in (".", ".."):
        p.error("--entry must be a filename prefix")

    out = Path(args.out).resolve()
    out.mkdir(parents=True)
    initrds = [Path(i).resolve() for i in args.initrd]
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])

    with (
        rootfs.rootfs("/buildroot", lowers=args.lower) as tree,
        tempfile.TemporaryDirectory(prefix="boot.") as scratch_dir,
    ):
        scratch = Path(scratch_dir)
        kvers = _kvers(tree)
        os_release = tree / "usr/lib/os-release"
        if not os_release.exists():
            raise SystemExit(
                "uki: the image ships no /usr/lib/os-release (ukify needs it) — install a release package"
            )
        efi_arch, stub_name = _ARCH[args.arch]
        stub = tree / "usr/lib/systemd/boot/efi" / stub_name
        if not stub.exists():
            raise SystemExit("uki: the image ships no systemd-boot stub — install systemd-boot-unsigned")

        base = _cmdline(
            args.cmdline,
            Path(args.root_hash).resolve() if args.root_hash else None,
            args.root_hash_kind,
        )
        cmdline = scratch / "cmdline"
        cmdline.write_text(base + "\x00")

        # Each profile becomes a small PE of .profile and .cmdline sections, joined into every
        # UKI below; the profile arguments extend the shared base cmdline.
        profile_pes = []
        addon_stub = tree / "usr/lib/systemd/boot/efi" / f"addon{efi_arch}.efi.stub"
        if args.profile and not addon_stub.exists():
            raise SystemExit("uki: the image ships no addon stub — install systemd-boot-unsigned")
        for value in args.profile:
            profile = json.loads(value)
            section = scratch / f"{profile['id']}.profile"
            section.write_text(f"ID={profile['id']}\nTITLE={profile['title']}\n")
            profile_cmdline = scratch / f"{profile['id']}.cmdline"
            profile_cmdline.write_text(" ".join(([base] if base else []) + profile["cmdline"]) + "\x00")
            pe = scratch / f"{profile['id']}.efi"
            cmd = [
                UKIFY, "build",
                "--profile", f"@{section}",
                "--cmdline", f"@{profile_cmdline}",
                "--stub", str(addon_stub),
                "--efi-arch", efi_arch,
                "--output", str(pe),
            ]  # fmt: skip
            subprocess.run(cmd, check=True)
            profile_pes.append(pe)

        for kver in kvers:
            modules = scratch / f"modules-{kver}.cpio"
            prefix = f"usr/lib/modules/{kver}"
            cpio.pack_tree(
                tree, modules, epoch,
                subtree=prefix,
                exclude=(f"{prefix}/vmlinuz*", f"{prefix}/vmlinux*", f"{prefix}/System.map"),
            )  # fmt: skip

            output = out / f"{args.entry}-{kver}.efi"
            cmd = [UKIFY, "build", "--linux", str(tree / prefix / "vmlinuz")]
            for initrd in [*initrds, modules]:
                cmd += ["--initrd", str(initrd)]
            cmd += [
                "--cmdline", f"@{cmdline}",
                *(argument for pe in profile_pes for argument in ("--join-profile", str(pe))),
                "--os-release", f"@{os_release}",
                "--uname", kver,
                "--stub", str(stub),
                "--efi-arch", efi_arch,
                "--output", str(output),
            ]  # fmt: skip
            subprocess.run(cmd, check=True)
            print(f"uki: built {output.name} (arch={args.arch})", file=sys.stderr)
    print(f"uki: built {len(kvers)} UKI(s) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
