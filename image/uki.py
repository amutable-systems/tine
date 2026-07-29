#!/usr/bin/python3
"""Build the unified kernel image for a logical filesystem image's single kernel.

Engine tools operate on the mounted image without chrooting. A kernel-modules cpio
extends the supplied base initrds.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TypedDict

import specs

import cpio
import finalize

# ukify lives outside PATH in the engine.
UKIFY = "/usr/lib/systemd/ukify"


class Profile(TypedDict):
    id: str
    title: str
    cmdline: list[str]
    sign_expected_pcr: bool


class RootHash(TypedDict):
    path: str
    kind: str


class SecureBoot(TypedDict):
    private_key: str
    certificate: str


class Spec(finalize.ImageSpec):
    out: str
    # Base initrd cpios, in load order.
    initrds: list[str]
    cmdline: list[str]
    profiles: list[Profile]
    root_hash: RootHash | None
    # ukify's EFI architecture (e.g. x64) and systemd's spelling in the UKI name (e.g. x86-64).
    efi_arch: str
    systemd_arch: str
    image_id: str
    version: str
    secure_boot: SecureBoot | None


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
    spec: Spec = specs.parse("uki", argv)

    efi_arch = spec["efi_arch"]
    out = Path(spec["out"]).resolve()
    out.mkdir(parents=True)
    initrds = [Path(initrd).resolve() for initrd in spec["initrds"]]
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    secure_boot = spec["secure_boot"]
    key = str(Path(secure_boot["private_key"]).resolve()) if secure_boot else None
    certificate = str(Path(secure_boot["certificate"]).resolve()) if secure_boot else None

    with (
        finalize.image(spec, program="uki") as tree,
        tempfile.TemporaryDirectory(prefix="boot.") as scratch_dir,
    ):
        scratch = Path(scratch_dir)
        kvers = _kvers(tree)
        if len(kvers) > 1:
            raise SystemExit(
                "uki: one image holds one kernel, found: "
                + ", ".join(kvers)
                + " — split kernel variants into separate images"
            )
        kver = kvers[0]
        os_release = tree / "usr/lib/os-release"
        if not os_release.exists():
            raise SystemExit(
                "uki: the image ships no /usr/lib/os-release (ukify needs it) — install a release package"
            )
        stub = tree / "usr/lib/systemd/boot/efi" / f"linux{efi_arch}.efi.stub"
        if not stub.exists():
            raise SystemExit("uki: the image ships no systemd-boot stub — install systemd-boot-unsigned")

        root_hash = spec["root_hash"]
        base = _cmdline(
            spec["cmdline"],
            Path(root_hash["path"]).resolve() if root_hash else None,
            root_hash["kind"] if root_hash else None,
        )
        cmdline = scratch / "cmdline"
        cmdline.write_text(base + "\x00")

        # Each profile becomes a small PE of .profile and .cmdline sections, joined into every
        # UKI below; the profile arguments extend the shared base cmdline.
        profiles = spec["profiles"]
        profile_pes = []
        addon_stub = tree / "usr/lib/systemd/boot/efi" / f"addon{efi_arch}.efi.stub"
        if profiles and not addon_stub.exists():
            raise SystemExit("uki: the image ships no addon stub — install systemd-boot-unsigned")
        for profile in profiles:
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

        # Secure Boot signing also seals the expected PCR 11 policy: ukify measures and signs the
        # base and each joined profile separately. All profiles are signed by default; the
        # explicit --sign-profile list is only needed when one opts out. The public key section
        # (.pcrpkey) derives from the private key, so no --pcr-certificate is needed.
        signing = []
        if key:
            assert certificate is not None  # the spec pairs the key and the certificate
            signing = [
                "--signtool", "systemd-sbsign",
                "--secureboot-private-key", key,
                "--secureboot-certificate", certificate,
                "--sign-kernel",
                "--pcr-banks", "sha256",
                "--pcr-private-key", key,
            ]  # fmt: skip
            if not all(profile["sign_expected_pcr"] for profile in profiles):
                signing += ["--sign-profile", "main"]
                for profile in profiles:
                    if profile["sign_expected_pcr"]:
                        signing += ["--sign-profile", profile["id"]]

        modules = scratch / f"modules-{kver}.cpio"
        prefix = f"usr/lib/modules/{kver}"
        cpio.pack_tree(
            tree, modules, epoch,
            subtree=prefix,
            exclude=(f"{prefix}/vmlinuz*", f"{prefix}/vmlinux*", f"{prefix}/System.map"),
        )  # fmt: skip

        output = out / f"{spec['image_id']}_{spec['version']}_{spec['systemd_arch']}.efi"
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
            *signing,
            "--output", str(output),
        ]  # fmt: skip
        subprocess.run(cmd, check=True)
    print(f"uki: built {output.name} -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
