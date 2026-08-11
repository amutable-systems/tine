"""Tests for kernel module selection and the explicit-path cpio packer.

    buck test tine//image:test

The pattern box, the firmware walk and the packing decide what a UKI carries, and none of them
need libkmod or a real kernel, so they run against synthetic module trees here. The dependency
closure libkmod resolves is covered by the real image builds in tools/ci.sh instead.
"""

import importlib.util
import json
import tempfile
import typing
import unittest
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace
from typing import override

HERE = Path(__file__).parent

# Fedora's paths for the modules this project's images actually boot through: erofs and dm-verity
# under a dissected /usr, on virtio in a VM and on NVMe or AHCI on metal.
BOOT_CRITICAL = (
    "kernel/fs/erofs/erofs.ko.xz",
    "kernel/drivers/md/dm-mod.ko.xz",
    "kernel/drivers/md/dm-verity.ko.xz",
    "kernel/drivers/block/virtio_blk.ko.xz",
    "kernel/drivers/virtio/virtio_pci.ko.xz",
    "kernel/drivers/scsi/virtio_scsi.ko.xz",
    "kernel/drivers/net/virtio_net.ko.xz",
    "kernel/drivers/block/loop.ko.xz",
    "kernel/drivers/nvme/host/nvme.ko.xz",
    "kernel/drivers/ata/ahci.ko.xz",
    "kernel/drivers/scsi/sd_mod.ko.xz",
    "kernel/fs/ext4/ext4.ko.xz",
    "kernel/fs/fat/vfat.ko.xz",
    "kernel/fs/overlayfs/overlay.ko.xz",
    "kernel/crypto/xor.ko.xz",
    "kernel/fs/nls/nls_cp437.ko.xz",
)

# Nothing an appliance needs before it has switched root.
NOT_CARRIED = (
    "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko.xz",
    "kernel/drivers/net/wireless/intel/iwlwifi/iwlwifi.ko.xz",
    "kernel/sound/pci/hda/snd-hda-intel.ko.xz",
    "kernel/drivers/crypto/qat/intel_qat.ko.xz",
)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_bzl(name: str) -> SimpleNamespace:
    path = HERE / f"{name}.bzl"
    module: dict[str, typing.Any] = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module)  # noqa: S102
    return SimpleNamespace(**module)


kmod = _load("kmod")
cpio = _load("cpio")
modules_bzl = _load_bzl("modules")


class TreeTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="kmod-test.")
        self.addCleanup(tmp.cleanup)
        self.tree = Path(tmp.name)
        self.kver = "6.99.0-1.fc99.x86_64"
        self.modulesd = self.tree / "usr/lib/modules" / self.kver
        self.modulesd.mkdir(parents=True)

    def install(self, *paths: str) -> None:
        for path in paths:
            target = self.modulesd / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x7fELF")

    def select(self, *patterns: str) -> list[str]:
        picked, _ = kmod.select(self.modulesd, list(patterns))
        return [str(rel) for rel in picked]


class TestSelect(TreeTest):
    def test_basename(self) -> None:
        self.install("kernel/fs/erofs/erofs.ko.xz", "kernel/fs/ext4/ext4.ko.xz")
        self.assertEqual(self.select("erofs"), ["kernel/fs/erofs/erofs.ko.xz"])

    def test_every_module_suffix(self) -> None:
        self.install(
            "kernel/a/one.ko",
            "kernel/a/two.ko.gz",
            "kernel/a/three.ko.xz",
            "kernel/a/four.ko.zst",
            "kernel/a/notamodule.bin",
        )
        self.assertEqual(len(self.select("*")), 4)

    def test_dash_and_underscore_are_one_character(self) -> None:
        self.install("kernel/drivers/block/virtio_blk.ko.xz", "kernel/drivers/md/dm-verity.ko.xz")
        self.assertEqual(self.select("virtio-blk"), ["kernel/drivers/block/virtio_blk.ko.xz"])
        self.assertEqual(self.select("dm_verity"), ["kernel/drivers/md/dm-verity.ko.xz"])

    def test_trailing_path_components(self) -> None:
        self.install("kernel/drivers/block/loop.ko.xz", "kernel/drivers/md/loop.ko.xz")
        self.assertEqual(self.select("block/loop"), ["kernel/drivers/block/loop.ko.xz"])

    def test_leading_slash_anchors_at_the_modules_root(self) -> None:
        self.install("kernel/fs/erofs/erofs.ko.xz")
        self.assertEqual(self.select("/kernel/fs/erofs/erofs"), ["kernel/fs/erofs/erofs.ko.xz"])
        # mkosi would retry this one under kernel/; tine does not.
        self.assertEqual(self.select("/fs/erofs/erofs"), [])

    def test_directory_takes_everything_below_it(self) -> None:
        self.install("kernel/crypto/xor.ko.xz", "kernel/crypto/deep/rc4.ko.xz", "kernel/fs/ext4/ext4.ko.xz")
        self.assertEqual(self.select("crypto/"), ["kernel/crypto/deep/rc4.ko.xz", "kernel/crypto/xor.ko.xz"])

    def test_character_class_keeps_its_members(self) -> None:
        self.install("kernel/drivers/md/raid0.ko.xz", "kernel/drivers/md/raid456.ko.xz", "kernel/x/rai.ko")
        self.assertEqual(len(self.select("raid[0-9]*")), 2)

    def test_last_match_wins(self) -> None:
        self.install("kernel/drivers/crypto/ccp/ccp_crypto.ko.xz", "kernel/drivers/crypto/qat/qat.ko.xz")
        self.assertEqual(
            self.select("crypto/", "-drivers/crypto/", "ccp_crypto"),
            ["kernel/drivers/crypto/ccp/ccp_crypto.ko.xz"],
        )
        # Reordered, the exclusion has the last word over both.
        self.assertEqual(self.select("crypto/", "ccp_crypto", "-drivers/crypto/"), [])

    def test_unmatched_patterns_are_reported(self) -> None:
        self.install("kernel/fs/erofs/erofs.ko.xz")
        _, unmatched = kmod.select(self.modulesd, ["erofs", "nosuchmodule", "-alsomissing"])
        self.assertEqual(unmatched, ["nosuchmodule", "-alsomissing"])

    def test_a_builtin_module_is_not_reported_as_unmatched(self) -> None:
        self.install("kernel/fs/erofs/erofs.ko.xz")
        (self.modulesd / "modules.builtin").write_text("kernel/fs/ext4/ext4.ko\n")
        picked, unmatched = kmod.select(self.modulesd, ["erofs", "ext4", "nosuchmodule"])
        # Nothing to pack for a module the kernel already holds, and nothing to warn about either.
        self.assertEqual([str(rel) for rel in picked], ["kernel/fs/erofs/erofs.ko.xz"])
        self.assertEqual(unmatched, ["nosuchmodule"])

    def test_nothing_is_selected_without_patterns(self) -> None:
        self.install("kernel/fs/erofs/erofs.ko.xz")
        self.assertEqual(self.select(), [])


class TestDefaultModules(TreeTest):
    """The pin: the shipped default must keep selecting what an image boots through."""

    def test_boot_critical_modules_are_selected(self) -> None:
        self.install(*BOOT_CRITICAL, *NOT_CARRIED)
        picked = self.select(*modules_bzl.DEFAULT_INITRD_MODULES)
        for module in BOOT_CRITICAL:
            with self.subTest(module=module):
                self.assertIn(module, picked)

    def test_the_rest_of_the_kernel_stays_out(self) -> None:
        self.install(*BOOT_CRITICAL, *NOT_CARRIED)
        picked = self.select(*modules_bzl.DEFAULT_INITRD_MODULES)
        for module in NOT_CARRIED:
            with self.subTest(module=module):
                self.assertNotIn(module, picked)


class TestFirmware(TreeTest):
    def firmware(self, path: str, content: bytes = b"fw") -> Path:
        target = self.tree / "usr/lib/firmware" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def files(self, *names: str) -> tuple[list[str], list[str]]:
        found, missing = kmod.firmware_files(self.tree, names)
        return sorted(str(path) for paths in found.values() for path in paths), missing

    def test_compressed_variant_is_found(self) -> None:
        self.firmware("amdgpu/vega.bin.xz")
        found, missing = self.files("amdgpu/vega.bin")
        self.assertEqual(found, ["usr/lib/firmware/amdgpu/vega.bin.xz"])
        self.assertEqual(missing, [])

    def test_missing_firmware_is_reported(self) -> None:
        self.firmware("other.bin")
        _, missing = self.files("absent.bin")
        self.assertEqual(missing, ["absent.bin"])

    def test_files_are_kept_under_the_name_that_asked_for_them(self) -> None:
        self.firmware("one.bin")
        self.firmware("two.bin.xz")
        found, _ = kmod.firmware_files(self.tree, ["one.bin", "two.bin"])
        # The manifest answers which module asked for a file through this mapping.
        self.assertEqual(
            {name: [str(path) for path in paths] for name, paths in found.items()},
            {"one.bin": ["usr/lib/firmware/one.bin"], "two.bin": ["usr/lib/firmware/two.bin.xz"]},
        )

    def test_symlinked_file_carries_its_target(self) -> None:
        self.firmware("real/qca.bin")
        link = self.firmware("alias.bin")
        link.unlink()
        link.symlink_to("real/qca.bin")
        found, _ = self.files("alias.bin")
        self.assertEqual(found, ["usr/lib/firmware/alias.bin", "usr/lib/firmware/real/qca.bin"])

    def test_symlinked_directory_component_is_carried(self) -> None:
        self.firmware("nvidia/ga102/gsp/booter.bin")
        (self.tree / "usr/lib/firmware/nvidia/ga103").symlink_to("ga102")
        found, _ = self.files("nvidia/ga103/gsp/booter.bin")
        self.assertEqual(
            found,
            ["usr/lib/firmware/nvidia/ga102/gsp/booter.bin", "usr/lib/firmware/nvidia/ga103"],
        )

    def test_relative_target_resolves_from_its_own_directory(self) -> None:
        self.firmware("vendor/blob.bin")
        link = self.firmware("other/blob.bin")
        link.unlink()
        link.symlink_to("../vendor/blob.bin")
        found, _ = self.files("other/blob.bin")
        self.assertEqual(found, ["usr/lib/firmware/other/blob.bin", "usr/lib/firmware/vendor/blob.bin"])

    def test_a_dangling_link_carries_no_absent_target(self) -> None:
        (self.tree / "usr/lib/firmware").mkdir(parents=True)
        (self.tree / "usr/lib/firmware/blob.bin").symlink_to("never/installed.bin")
        found, _ = self.files("blob.bin")
        self.assertEqual(found, ["usr/lib/firmware/blob.bin"])
        # Whatever comes back has to be packable, which an absent path is not.
        cpio.pack_paths(self.tree, self.tree / "out.cpio", 0, found)

    def test_a_firmware_name_cannot_leave_the_firmware_tree(self) -> None:
        self.firmware("real.bin")
        for name in ("../../../etc/shadow", "/etc/shadow"):
            with self.subTest(name=name), self.assertRaises(SystemExit):
                kmod.firmware_files(self.tree, [name])

    def test_a_symlink_loop_fails_instead_of_spinning(self) -> None:
        (self.tree / "usr/lib/firmware").mkdir(parents=True)
        (self.tree / "usr/lib/firmware/a.bin").symlink_to("b.bin")
        (self.tree / "usr/lib/firmware/b.bin").symlink_to("a.bin")
        with self.assertRaises(SystemExit):
            kmod.firmware_files(self.tree, ["a.bin"])


class TestCarry(TreeTest):
    """Module directories link modules at each other; a link without its target loads nothing."""

    def test_a_linked_module_brings_its_target(self) -> None:
        self.install("kernel/drivers/md/dm-verity.ko.xz")
        link = self.modulesd / "weak-updates/dm-verity.ko.xz"
        link.parent.mkdir()
        link.symlink_to("../kernel/drivers/md/dm-verity.ko.xz")
        rel = PurePosixPath(f"usr/lib/modules/{self.kver}/weak-updates/dm-verity.ko.xz")
        self.assertEqual(
            sorted(str(path) for path in kmod.carry(self.tree, rel)),
            [
                f"usr/lib/modules/{self.kver}/kernel/drivers/md/dm-verity.ko.xz",
                f"usr/lib/modules/{self.kver}/weak-updates/dm-verity.ko.xz",
            ],
        )

    def test_a_plain_file_carries_only_itself(self) -> None:
        self.install("kernel/fs/erofs/erofs.ko.xz")
        rel = PurePosixPath(f"usr/lib/modules/{self.kver}/kernel/fs/erofs/erofs.ko.xz")
        self.assertEqual(kmod.carry(self.tree, rel), {rel})


class TestEntries(TreeTest):
    def build(self, *modules: str, firmware: Sequence[str] = ()) -> list[typing.Any]:
        return kmod.entries(
            self.tree,
            self.kver,
            {PurePosixPath(path): kmod.Entry(PurePosixPath(path), "module") for path in modules},
            {PurePosixPath(path): kmod.Entry(PurePosixPath(path), "firmware") for path in firmware},
        )

    def entries(self, *modules: str, firmware: Sequence[str] = ()) -> list[str]:
        return [str(entry.path) for entry in self.build(*modules, firmware=firmware)]

    def kinds(self, *modules: str, firmware: Sequence[str] = ()) -> dict[str, str]:
        return {str(entry.path): entry.kind for entry in self.build(*modules, firmware=firmware)}

    def test_every_entry_says_what_it_is(self) -> None:
        self.install("kernel/fs/erofs/erofs.ko.xz")
        (self.modulesd / "modules.dep").write_text("")
        (self.modulesd / "vdso").mkdir()
        (self.modulesd / "vdso/vdso64.so").write_bytes(b"")
        (self.tree / "usr/lib/firmware").mkdir(parents=True)
        (self.tree / "usr/lib/firmware/blob.bin").write_bytes(b"fw")
        base = f"usr/lib/modules/{self.kver}"
        kinds = self.kinds(f"{base}/kernel/fs/erofs/erofs.ko.xz", firmware=["usr/lib/firmware/blob.bin"])
        self.assertEqual(kinds[f"{base}/kernel/fs/erofs/erofs.ko.xz"], "module")
        self.assertEqual(kinds["usr/lib/firmware/blob.bin"], "firmware")
        self.assertEqual(kinds[f"{base}/modules.dep"], "index")
        self.assertEqual(kinds[f"{base}/vdso/vdso64.so"], "vdso")
        self.assertEqual(kinds["usr/lib/modules"], "directory")

    def test_sizes_are_recorded_for_everything_but_directories(self) -> None:
        self.install("kernel/a/one.ko")
        entries = {
            entry.path.name: entry for entry in self.build(f"usr/lib/modules/{self.kver}/kernel/a/one.ko")
        }
        self.assertEqual(entries["one.ko"].size, len(b"\x7fELF"))
        self.assertIsNone(entries["usr"].size)

    def test_indexes_and_parents_come_along(self) -> None:
        self.install("kernel/fs/erofs/erofs.ko.xz")
        (self.modulesd / "modules.dep").write_text("")
        (self.modulesd / "modules.alias.bin").write_bytes(b"")
        (self.modulesd / "vmlinuz").write_bytes(b"")
        entries = self.entries(f"usr/lib/modules/{self.kver}/kernel/fs/erofs/erofs.ko.xz")
        self.assertIn("usr", entries)
        self.assertIn("usr/lib/modules", entries)
        self.assertIn(f"usr/lib/modules/{self.kver}/modules.dep", entries)
        self.assertIn(f"usr/lib/modules/{self.kver}/modules.alias.bin", entries)
        # The kernel is its own PE section; a copy in the initrd is dead weight.
        self.assertNotIn(f"usr/lib/modules/{self.kver}/vmlinuz", entries)

    def test_vdso_comes_along(self) -> None:
        (self.modulesd / "vdso").mkdir()
        (self.modulesd / "vdso/vdso64.so").write_bytes(b"")
        entries = self.entries()
        self.assertIn(f"usr/lib/modules/{self.kver}/vdso", entries)
        self.assertIn(f"usr/lib/modules/{self.kver}/vdso/vdso64.so", entries)

    def test_a_parent_always_precedes_its_children(self) -> None:
        self.install("kernel/fs/erofs/erofs.ko.xz")
        entries = self.entries(f"usr/lib/modules/{self.kver}/kernel/fs/erofs/erofs.ko.xz")
        for index, entry in enumerate(entries):
            for parent in PurePosixPath(entry).parents:
                if str(parent) != "." and str(parent) in entries:
                    self.assertLess(entries.index(str(parent)), index)


class TestManifest(unittest.TestCase):
    """The report shipped beside the UKI has to answer why each module is in there, and what is not."""

    def render(self) -> dict[str, typing.Any]:
        modulesd = PurePosixPath("usr/lib/modules/6.99.0")
        selection = kmod.Selection(
            kernel="6.99.0",
            patterns=["erofs", "nosuchmodule"],
            entries=[
                kmod.Entry(PurePosixPath("usr/lib/modules"), "directory"),
                kmod.Entry(modulesd / "kernel/fs/erofs/erofs.ko.xz", "module", 40, selected=True),
                kmod.Entry(modulesd / "kernel/lib/lz4.ko.xz", "module", 20, needed_by=("erofs",)),
                kmod.Entry(modulesd / "modules.dep", "index", 5),
                kmod.Entry(
                    PurePosixPath("usr/lib/firmware/blob.bin"), "firmware", 7, declared_by=("ath10k",)
                ),
            ],
            modules=[modulesd / "kernel/fs/erofs/erofs.ko.xz", modulesd / "kernel/lib/lz4.ko.xz"],
            firmware=[PurePosixPath("usr/lib/firmware/blob.bin")],
            unmatched=["nosuchmodule"],
            missing=["absent-dependency"],
            missing_firmware=["absent.bin"],
        )
        return json.loads(kmod.manifest(selection, archive_bytes=4096))

    def test_it_records_what_did_not_make_it(self) -> None:
        report = self.render()
        self.assertEqual(report["kernel"], "6.99.0")
        self.assertEqual(report["patterns"], ["erofs", "nosuchmodule"])
        self.assertEqual(report["unmatched_patterns"], ["nosuchmodule"])
        self.assertEqual(report["missing_modules"], ["absent-dependency"])
        self.assertEqual(report["missing_firmware"], ["absent.bin"])

    def test_it_records_why_each_entry_is_there(self) -> None:
        entries = {entry["path"]: entry for entry in self.render()["entries"]}
        selected = entries["usr/lib/modules/6.99.0/kernel/fs/erofs/erofs.ko.xz"]
        self.assertEqual(selected["kind"], "module")
        self.assertTrue(selected["selected"])
        self.assertNotIn("needed_by", selected)
        self.assertEqual(entries["usr/lib/modules/6.99.0/kernel/lib/lz4.ko.xz"]["needed_by"], ["erofs"])
        self.assertEqual(entries["usr/lib/firmware/blob.bin"]["declared_by"], ["ath10k"])
        # A directory carries no payload, so it claims no size.
        self.assertNotIn("size", entries["usr/lib/modules"])

    def test_it_totals_the_bytes_it_carries(self) -> None:
        totals = self.render()["totals"]
        self.assertEqual(totals, {
            "modules": 2,
            "firmware": 1,
            "entries": 5,
            "content_bytes": 72,
            "archive_bytes": 4096,
        })  # fmt: skip


class TestPackPaths(TreeTest):
    def pack(self, paths: list[str]) -> Path:
        out = self.tree / "out.cpio"
        cpio.pack_paths(self.tree, out, 0, paths)
        return out

    def names(self, archive: Path) -> list[str]:
        return [entry.name for entry in cpio.read(archive.read_bytes())]

    def test_packs_exactly_what_it_is_given(self) -> None:
        self.install("kernel/a/one.ko", "kernel/a/two.ko")
        base = f"usr/lib/modules/{self.kver}/kernel/a"
        self.assertEqual(self.names(self.pack([f"{base}/one.ko"])), [f"{base}/one.ko"])

    def test_input_order_and_repeats_do_not_matter(self) -> None:
        self.install("kernel/a/one.ko")
        base = f"usr/lib/modules/{self.kver}"
        paths = [f"{base}/kernel/a/one.ko", f"{base}/kernel", f"{base}/kernel/a"]
        first = self.pack(paths).read_bytes()
        second = self.pack([*reversed(paths), *paths]).read_bytes()
        self.assertEqual(first, second)
        self.assertEqual(
            self.names(self.pack(paths)),
            [f"{base}/kernel", f"{base}/kernel/a", f"{base}/kernel/a/one.ko"],
        )

    def test_symlinks_stay_symlinks(self) -> None:
        self.install("kernel/a/one.ko")
        (self.modulesd / "link.ko").symlink_to("kernel/a/one.ko")
        base = f"usr/lib/modules/{self.kver}"
        entries = list(cpio.read(self.pack([f"{base}/link.ko"]).read_bytes()))
        self.assertEqual(bytes(entries[0].data), b"kernel/a/one.ko")


if __name__ == "__main__":
    unittest.main()
