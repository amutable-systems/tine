"""Tests for the release listing.

buck test tine//image:test
"""

import json
import tempfile
import unittest
from pathlib import Path

import publish

HASH = "0123456789abcdef" * 4
OTHER = "fedcba9876543210" * 4


class TestListing(unittest.TestCase):
    def test_kinds_images_and_root_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disk_hash = Path(tmp) / "roothash"
            disk_hash.write_text(HASH + "\n", encoding="utf-8")
            ext_hash = Path(tmp) / "ext.sysext.roothash"
            ext_hash.write_text(OTHER, encoding="utf-8")
            files: dict[str, publish.Entry] = {
                "img.efi": {"image": "img", "kind": "uki", "path": "/x/img.efi"},
                "img.usr-x86-64.0123.raw": {"image": "img", "kind": "usr", "path": "/x/usr.raw"},
                "img.usr-x86-64-verity.fedc.raw": {
                    "image": "img",
                    "kind": "usr-verity",
                    "path": "/x/verity.raw",
                },
                "img.initrd.spdx.json": {
                    "image": "img.initrd",
                    "kind": "sbom",
                    "path": "/x/initrd.spdx.json",
                },
                "ext.sysext.raw": {"image": "ext", "kind": "sysext", "path": "/x/ext.raw"},
                "ext.sysext.roothash": {"image": "ext", "kind": "roothash", "path": str(ext_hash)},
            }
            spec: publish.Spec = {
                "arch": "x86-64",
                "files": files,
                "out": "unused",
                "root_hashes": {"img": str(disk_hash)},
            }
            self.assertEqual(
                publish.listing(spec),
                {
                    "arch": "x86-64",
                    "files": {
                        "ext.sysext.raw": {"image": "ext", "kind": "sysext", "root_hash": OTHER},
                        "ext.sysext.roothash": {"image": "ext", "kind": "roothash"},
                        "img.efi": {"image": "img", "kind": "uki"},
                        "img.initrd.spdx.json": {"image": "img.initrd", "kind": "sbom"},
                        "img.usr-x86-64.0123.raw": {"image": "img", "kind": "usr", "root_hash": HASH},
                        "img.usr-x86-64-verity.fedc.raw": {"image": "img", "kind": "usr-verity"},
                    },
                },
            )

    def test_disk_without_verity_and_extension_without_sidecar(self) -> None:
        files: dict[str, publish.Entry] = {
            "img.usr-x86-64.0123.raw": {"image": "img", "kind": "usr", "path": "/x/usr.raw"},
            "ext.sysext.raw": {"image": "ext", "kind": "sysext", "path": "/x/ext.raw"},
        }
        self.assertIsNone(publish.root_hash("img.usr-x86-64.0123.raw", files, {}))
        self.assertIsNone(publish.root_hash("ext.sysext.raw", files, {}))

    def test_rejects_a_bad_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "roothash"
            bad.write_text("not a hash\n", encoding="utf-8")
            files: dict[str, publish.Entry] = {
                "img.usr-x86-64.0123.raw": {"image": "img", "kind": "usr", "path": "/x/usr.raw"}
            }
            with self.assertRaises(SystemExit):
                publish.root_hash("img.usr-x86-64.0123.raw", files, {"img": str(bad)})

    def test_rejects_two_usr_partitions_for_one_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disk_hash = Path(tmp) / "roothash"
            disk_hash.write_text(HASH, encoding="utf-8")
            files: dict[str, publish.Entry] = {
                "img.usr-a.raw": {"image": "img", "kind": "usr", "path": "/x/a.raw"},
                "img.usr-b.raw": {"image": "img", "kind": "usr", "path": "/x/b.raw"},
            }
            with self.assertRaises(SystemExit):
                publish.root_hash("img.usr-a.raw", files, {"img": str(disk_hash)})

    def test_rejects_two_root_hash_files(self) -> None:
        files: dict[str, publish.Entry] = {
            "ext.sysext.raw": {"image": "ext", "kind": "sysext", "path": "/x/ext.raw"},
            "ext.a.roothash": {"image": "ext", "kind": "roothash", "path": "/x/a"},
            "ext.b.roothash": {"image": "ext", "kind": "roothash", "path": "/x/b"},
        }
        with self.assertRaises(SystemExit):
            publish.root_hash("ext.sysext.raw", files, {})

    def test_main_writes_the_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "artifacts_x86-64.json"
            spec = Path(tmp) / "spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "arch": "x86-64",
                        "files": {"img.efi": {"image": "img", "kind": "uki", "path": "/x/img.efi"}},
                        "out": str(out),
                        "root_hashes": {},
                    }
                ),
                encoding="utf-8",
            )
            publish.main(["--spec", str(spec)])
            self.assertEqual(
                json.loads(out.read_text(encoding="utf-8")),
                {"arch": "x86-64", "files": {"img.efi": {"image": "img", "kind": "uki"}}},
            )


if __name__ == "__main__":
    unittest.main()
