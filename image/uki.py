#!/usr/bin/python3
"""Build a standalone unified kernel image from a logical filesystem image.

Engine tools operate on the mounted image without chrooting. The per-kernel modules cpio
joins the supplied base initrds.
"""

import argparse
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


def _kver(tree: Path, requested: str | None) -> str:
    modules = tree / "usr/lib/modules"
    if requested is not None:
        if not (modules / requested / "vmlinuz").exists():
            raise SystemExit(f"uki: kernel {requested!r} has no usr/lib/modules/<version>/vmlinuz")
        return requested
    kvers = [p.name for p in modules.iterdir() if (p / "vmlinuz").exists()] if modules.is_dir() else []
    if len(kvers) != 1:
        raise SystemExit(f"uki: expected exactly one kernel under /usr/lib/modules, found {kvers or 'none'}")
    return kvers[0]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="uki")
    p.add_argument("--lower", action="append", default=[], help="the image stack (bottom..top) to merge")
    p.add_argument("--out", required=True, help="the output unified kernel image")
    p.add_argument(
        "--initrd", action="append", default=[], help="base initrd cpio, in load order (repeatable)"
    )
    p.add_argument("--cmdline", default="", help="kernel command line embedded in the UKI")
    p.add_argument("--arch", required=True, choices=tuple(_ARCH))
    p.add_argument("--kernel-version", help="kernel release to use")
    args = p.parse_args(argv)

    out = Path(args.out).resolve()
    initrds = [Path(i).resolve() for i in args.initrd]
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])

    with (
        rootfs.rootfs("/buildroot", lowers=args.lower) as tree,
        tempfile.TemporaryDirectory(prefix="boot.") as scratch_dir,
    ):
        scratch = Path(scratch_dir)
        kver = _kver(tree, args.kernel_version)
        os_release = tree / "usr/lib/os-release"
        if not os_release.exists():
            raise SystemExit(
                "uki: the image ships no /usr/lib/os-release (ukify needs it) — install a release package"
            )
        efi_arch, stub_name = _ARCH[args.arch]
        stub = tree / "usr/lib/systemd/boot/efi" / stub_name
        if not stub.exists():
            raise SystemExit("uki: the image ships no systemd-boot stub — install systemd-boot-unsigned")

        modules = scratch / "modules.cpio"
        prefix = f"usr/lib/modules/{kver}"
        cpio.pack_tree(
            tree, modules, epoch,
            subtree=prefix,
            exclude=(f"{prefix}/vmlinuz*", f"{prefix}/vmlinux*", f"{prefix}/System.map"),
        )  # fmt: skip

        cmdline = scratch / "cmdline"
        cmdline.write_text(args.cmdline + "\x00")

        cmd = [UKIFY, "build", "--linux", str(tree / prefix / "vmlinuz")]
        for initrd in [*initrds, modules]:
            cmd += ["--initrd", str(initrd)]
        cmd += [
            "--cmdline", f"@{cmdline}",
            "--os-release", f"@{os_release}",
            "--uname", kver,
            "--stub", str(stub),
            "--efi-arch", efi_arch,
            "--output", str(out),
        ]  # fmt: skip
        subprocess.run(cmd, check=True)
    print(f"uki: built {out.name} (kver={kver}, arch={args.arch})", file=sys.stderr)


if __name__ == "__main__":
    main()
