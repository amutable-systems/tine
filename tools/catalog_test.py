# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the catalog refresher.

    buck test tine//tools:catalog-test

The gateway enumeration is stubbed, so advancing a pin is covered offline. Armoring a key is checked
against the dev box's gpg, and a failed refresh's rollback against a scratch checkout.
"""

import base64
import contextlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

import catalog

MIRROR = "https://mirror.test/v2/mirror/public/t9"

# What each fixture repository's manifest says it is pinned to, per architecture, in place of
# building the manifest: (target, architecture) -> snapshot id as the mirror spells it.
SERVED: dict[tuple[str, str], str] = {}


def pin(snapshot: str, name: str = "test.rolling", **snapshots: str) -> catalog.Pin:
    """A repository's pin as `_pinned_repositories` reads it, serving `snapshots` per architecture."""
    target = f"tine//catalog:{name}.repository"
    for architecture, served in snapshots.items():
        SERVED[(target, architecture)] = served
    return catalog.Pin(
        target=target,
        mirror=MIRROR,
        snapshot=snapshot,
        architectures=tuple(snapshots),
    )


def two_architectures(snapshot: str) -> catalog.Pin:
    """A pin whose mirror serves arm64 and x86_64, each under rpm's name for it."""
    return pin(
        snapshot,
        arm64=snapshot.replace("$basearch", "aarch64"),
        x86_64=snapshot.replace("$basearch", "x86_64"),
    )


class TestCheckout(unittest.TestCase):
    def test_puts_back_what_it_wrote_and_removes_what_it_created(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            existing = Path(scratch) / "BUCK"
            existing.write_text("pin = 1\n")
            created = Path(scratch) / "new.json"
            checkout = catalog._Checkout()
            checkout.write(existing, "pin = 2\n")
            checkout.write(existing, "pin = 3\n")
            checkout.write(created, "{}\n")
            checkout.restore()
            self.assertEqual(existing.read_text(), "pin = 1\n")
            self.assertFalse(created.exists())
            # Restored once, a second restore has nothing left to undo.
            existing.write_text("pin = 4\n")
            checkout.restore()
            self.assertEqual(existing.read_text(), "pin = 4\n")

    def test_restores_the_rest_when_one_file_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            gone = Path(scratch) / "gone" / "lock.json"
            gone.parent.mkdir()
            gone.write_text("old\n")
            kept = Path(scratch) / "BUCK"
            kept.write_text("old\n")
            checkout = catalog._Checkout()
            checkout.write(gone, "new\n")
            checkout.write(kept, "new\n")
            # A file where the directory was: nothing can be written back under it.
            gone.unlink()
            gone.parent.rmdir()
            gone.parent.write_text("")
            with self.assertRaises(OSError):
                checkout.restore()
            self.assertEqual(kept.read_text(), "old\n")


class TestArmor(unittest.TestCase):
    def test_matches_the_reference_check_value(self) -> None:
        # CRC-24/OPENPGP's published check value for "123456789".
        self.assertEqual(catalog._crc24(b"123456789"), 0x21CF02)

    def test_wraps_a_binary_key_the_way_gpg_reads_it(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch) / "gnupg"
            home.mkdir(mode=0o700)
            gpg = ["gpg", "--homedir", str(home), "--batch", "--quiet"]
            subprocess.run(
                [
                    *gpg,
                    "--passphrase",
                    "",
                    "--pinentry-mode",
                    "loopback",
                    "--quick-generate-key",
                    "k",
                    "ed25519",
                ],
                check=True,
            )
            binary = subprocess.run([*gpg, "--export"], check=True, stdout=subprocess.PIPE).stdout
            subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], check=True)
            armored = catalog.armor(binary)
            path = Path(scratch) / "key.asc"
            path.write_text(armored)
            shown = subprocess.run(
                [*gpg, "--show-keys", "--with-colons", str(path)],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout
            listed = subprocess.run(
                [*gpg, "--list-keys", "--with-colons"], check=True, stdout=subprocess.PIPE, text=True
            ).stdout

        def fingerprint(colons: str) -> str:
            return next(line for line in colons.splitlines() if line.startswith("fpr:"))

        self.assertEqual(fingerprint(shown), fingerprint(listed))
        self.assertTrue(armored.startswith(catalog.ARMOR_HEADER.decode() + "\n\n"))
        self.assertTrue(armored.endswith(catalog.ARMOR_FOOTER.decode() + "\n"))
        body = "".join(line for line in armored.splitlines()[2:-2])
        self.assertEqual(base64.b64decode(body), binary)


class NewestRpmrepoSnapshot(unittest.TestCase):
    @override
    def setUp(self) -> None:
        # Where the tool would build a manifest, answer from the fixtures.
        patched = mock.patch.object(
            catalog,
            "_pinned_snapshot",
            side_effect=lambda buck, pin, architecture: SERVED[(pin.target, architecture)],
        )
        patched.start()
        self.addCleanup(patched.stop)

    def enumerating(
        self, *snapshots: str
    ) -> contextlib.AbstractContextManager[mock.MagicMock | mock.AsyncMock]:
        """Stub the mirror, answering each series it is asked about with the next snapshot given."""
        return mock.patch.object(catalog, "_newest_snapshot", side_effect=snapshots)

    def test_moves_the_datestamp_and_keeps_the_placeholder(self) -> None:
        with self.enumerating("t9-x86_64-rolling-20260202"):
            newest = catalog._newest_rpmrepo_snapshot(
                "buck", [pin("t9-$basearch-rolling-20260101", x86_64="t9-x86_64-rolling-20260101")]
            )
        self.assertEqual(newest, "t9-$basearch-rolling-20260202")

    def test_enumerates_the_series_of_every_architecture(self) -> None:
        with self.enumerating(
            "t9-aarch64-rolling-20260202", "t9-x86_64-rolling-20260202"
        ) as newest_snapshot:
            catalog._newest_rpmrepo_snapshot("buck", [two_architectures("t9-$basearch-rolling-20260101")])
        self.assertEqual(
            [call.args[2] for call in newest_snapshot.call_args_list],
            ["t9-aarch64-rolling", "t9-x86_64-rolling"],
        )

    def test_refuses_architectures_on_different_days(self) -> None:
        with self.enumerating("t9-aarch64-rolling-20260201", "t9-x86_64-rolling-20260202"):
            with self.assertRaises(SystemExit) as raised:
                catalog._newest_rpmrepo_snapshot(
                    "buck", [two_architectures("t9-$basearch-rolling-20260101")]
                )
        self.assertIn("one pin cannot advance to several snapshots", str(raised.exception))
        self.assertIn("(arm64) offers t9-aarch64-rolling-20260201", str(raised.exception))

    def test_refuses_repositories_on_different_days(self) -> None:
        shared = "t9-$basearch-rolling-20260101"
        with self.enumerating("t9-x86_64-rolling-20260201", "t9-x86_64-rolling-20260202"):
            with self.assertRaises(SystemExit) as raised:
                catalog._newest_rpmrepo_snapshot(
                    "buck",
                    [
                        pin(shared, name="test.core", x86_64="t9-x86_64-rolling-20260101"),
                        pin(shared, name="test.extra", x86_64="t9-x86_64-rolling-20260101"),
                    ],
                )
        self.assertIn("one pin cannot advance to several snapshots", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
