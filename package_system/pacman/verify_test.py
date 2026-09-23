# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the signature verifier against the real `gpg` binary.

A throwaway distribution: main keys certify packager keys, and the keyring package's files are
built from them the way archlinux-keyring's are. It lives at a fixed date, the way a pinned
snapshot does, so what expires and when is under the test's control.
"""

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import override

import alpm
import keyring
import verify
from gnupg import KEYRING_FILES, fingerprints, gpg, kill_agent

EPOCH = 1739577600

# The distribution's keys are made and its packages signed on the first day; the snapshot the
# tests verify against is pinned to the third.
MADE = "20260101T000000"
SIGNED = "20260102T000000"
PINNED = "2026-01-03T00:00:00Z"
LATER = "2026-02-01T00:00:00Z"


class Distribution:
    """Main and packager keys in one gpg home, exported the way the keyring package ships them."""

    def __init__(self, home: Path) -> None:
        self.home = home
        home.mkdir(mode=0o700)
        self.main = [self._generate(f"main{n}") for n in range(4)]
        # Three main keys vouch for `vouched`, two for `weak`, none for `stray`; `stale` is vouched
        # for but expires a week after it was made. Packagers sign with a subkey, as Arch's do.
        self.vouched = self._generate("vouched", subkey=True)
        self.weak = self._generate("weak", subkey=True)
        self.stray = self._generate("stray", subkey=True)
        self.stale = self._generate("stale", subkey=True, expire="7d")
        for packager, certifiers in (
            (self.vouched, self.main[:3]),
            (self.weak, self.main[:2]),
            (self.stale, self.main[:3]),
        ):
            for main in certifiers:
                self.gpg("--yes", "--local-user", main, "--quick-sign-key", packager)

    def gpg(self, *args: str, home: Path | None = None, at: str = MADE, stdin: str | None = None) -> str:
        command = ("--faked-system-time", at, "--quiet", *args)
        return gpg(home or self.home, *command, stdin=stdin, capture=True, check=True).stdout

    def _generate(self, name: str, subkey: bool = False, expire: str = "never") -> str:
        self.gpg(
            "--passphrase", "", "--pinentry-mode", "loopback",
            "--quick-generate-key", f"{name} <{name}@tine.test>", "ed25519", "sign", expire,
        )  # fmt: skip
        fingerprint = fingerprints(self.home, "--list-keys", f"{name}@tine.test")[0]
        if subkey:
            self.gpg(
                "--passphrase",
                "",
                "--pinentry-mode",
                "loopback",
                "--quick-add-key",
                fingerprint,
                "ed25519",
                "sign",
                expire,
            )
        return fingerprint

    def signing_key(self, fingerprint: str) -> str:
        """What to sign with: the subkey where the key has one, else the primary."""
        listing = self.gpg("--with-colons", "--list-keys", fingerprint)
        after_sub = False
        for line in listing.splitlines():
            fields = line.split(":")
            if fields[0] == "sub":
                after_sub = True
            elif fields[0] == "fpr" and after_sub:
                return fields[9] + "!"
        return fingerprint

    def key_file(self, fingerprint: str, directory: Path, secret: bool = False) -> Path:
        path = directory / f"{fingerprint}.key"
        export = "--export-secret-keys" if secret else "--export"
        path.write_text(
            self.gpg("--passphrase", "", "--pinentry-mode", "loopback", export, "--armor", fingerprint)
        )
        return path

    def keyrings(
        self, directory: Path, revoked: tuple[str, ...] = (), revocations: tuple[str, ...] = ()
    ) -> Path:
        """The keyring package's files: every public key, and the withdrawn fingerprints.

        `revocations` names keys whose own revocation certificate the keyring carries, applied to a
        copy so the distribution itself keeps them.
        """
        directory.mkdir()
        home = self.home
        if revocations:
            # A fresh home holding the public keys, so the distribution itself keeps its keys.
            home = directory / "revoking"
            home.mkdir(mode=0o700)
            self.gpg("--import", home=home, stdin=self.gpg("--export", "--armor"))
            for fingerprint in revocations:
                # The certificate gpg generated alongside the key, its armor header prefixed with a
                # colon that guards it against being imported by accident.
                lines = (self.home / "openpgp-revocs.d" / f"{fingerprint}.rev").read_text().splitlines()
                self.gpg("--import", home=home, stdin="\n".join(line.removeprefix(":") for line in lines))
        self.gpg("--output", str(directory / f"{keyring.KEYRING}.gpg"), "--export", home=home)
        (directory / f"{keyring.KEYRING}-revoked").write_text("".join(f"{f}\n" for f in revoked))
        return directory

    def sign(self, signer: str, package: Path) -> str:
        """The base64 detached signature a database entry carries."""
        signature = package.with_name(package.name + ".sig")
        signer = self.signing_key(signer)
        self.gpg("--yes", "-u", signer, "--detach-sign", "--output", str(signature), str(package), at=SIGNED)
        return base64.b64encode(signature.read_bytes()).decode()


class TestVerify(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls._scratch = tempfile.TemporaryDirectory()
        scratch = Path(cls._scratch.name)
        cls.distribution = Distribution(scratch / "distribution")
        cls.addClassCleanup(kill_agent, cls.distribution.home)
        cls.packages = scratch / "pool"
        cls.packages.mkdir()

    def package(
        self, name: str, signer: str | None, content: bytes | None = None
    ) -> tuple[Path, dict[str, list[str]]]:
        """A pool artifact named by checksum, and the database entry describing it."""
        content = content if content is not None else f"{name} contents".encode()
        checksum = hashlib.sha256(content).hexdigest()
        path = self.packages / f"{checksum}.pkg.tar.zst"
        path.write_bytes(content)
        entry = {
            "FILENAME": [f"{name}-1-1-any.pkg.tar.zst"],
            "NAME": [name],
            "VERSION": ["1-1"],
            "CSIZE": [str(len(content))],
            "SHA256SUM": [checksum],
        }
        if signer is not None:
            entry["PGPSIG"] = [self.distribution.sign(signer, path)]
        return path, entry

    def repository(self, *entries: dict[str, list[str]]) -> Path:
        directory = Path(tempfile.mkdtemp(dir=self._scratch.name)) / "repo"
        directory.mkdir()
        alpm.write_db([(f"{e['NAME'][0]}-1-1", e) for e in entries], directory / "test.db", EPOCH)
        return directory

    def keyring(
        self,
        *declared: str,
        files: dict[str, Path] | None = None,
        revoked: tuple[str, ...] = (),
        revocations: tuple[str, ...] = (),
        time: str | None = PINNED,
    ) -> Path:
        scratch = Path(tempfile.mkdtemp(dir=self._scratch.name))
        if files is None:
            files = {f: self.distribution.key_file(f, scratch) for f in declared}
        out = scratch / "keyring"
        spec = keyring.Spec(keys={f: str(path) for f, path in files.items()}, out=str(out), time=time)
        keyring.keyring(
            spec, keyrings=self.distribution.keyrings(scratch / "keyrings", revoked, revocations)
        )
        return out

    def verify(self, keyring: Path, repository: Path, *packages: Path) -> Path:
        # A build shares one keyring between verifications, so none of them may change it. The
        # sandbox runs as root, which write bits would not stop, so what is compared is the bytes.
        before = {path.name: path.read_bytes() for path in keyring.iterdir()}
        out = keyring.parent / "verified"
        try:
            verify.verify(
                verify.Spec(
                    keyring=str(keyring),
                    out=str(out),
                    packages={f"pkg--{p.name}": str(p) for p in packages},
                    repository=str(repository),
                )
            )
        finally:
            self.assertEqual({path.name: path.read_bytes() for path in keyring.iterdir()}, before)
        return out

    def test_keyring_holds_keys_and_validity_and_nothing_secret(self) -> None:
        keyring = self.keyring(*self.distribution.main)
        self.assertEqual(sorted(path.name for path in keyring.iterdir()), sorted(KEYRING_FILES))
        listing = gpg(
            keyring, "--with-colons", "--list-keys", self.distribution.vouched, capture=True, check=True
        )
        self.assertIn("pub:f:", listing.stdout)

    def test_keyring_refuses_a_key_file_under_another_fingerprint(self) -> None:
        main = self.distribution.main
        scratch = Path(tempfile.mkdtemp(dir=self._scratch.name))
        with self.assertRaises(SystemExit) as failure:
            self.keyring(files={main[1]: self.distribution.key_file(main[0], scratch)})
        self.assertIn(f"holds ['{main[0]}'], not the declared key {main[1]}", str(failure.exception))

    def test_keyring_refuses_a_key_file_that_is_no_key(self) -> None:
        scratch = Path(tempfile.mkdtemp(dir=self._scratch.name))
        (scratch / "junk.key").write_text("<html>Access Denied</html>\n")
        with self.assertRaises(SystemExit) as failure:
            self.keyring(files={self.distribution.main[0]: scratch / "junk.key"})
        self.assertIn("is not a key file", str(failure.exception))

    def test_keyring_refuses_a_key_file_carrying_a_secret_key(self) -> None:
        main = self.distribution.main[0]
        scratch = Path(tempfile.mkdtemp(dir=self._scratch.name))
        with self.assertRaises(SystemExit) as failure:
            self.keyring(files={main: self.distribution.key_file(main, scratch, secret=True)})
        self.assertIn("carries a secret key", str(failure.exception))

    def test_keyring_refuses_a_declared_key_the_package_revoked(self) -> None:
        with self.assertRaises(SystemExit) as failure:
            self.keyring(*self.distribution.main[:3], revoked=(self.distribution.main[0],))
        self.assertIn("are revoked by", str(failure.exception))

    def test_publishes_a_package_three_declared_main_keys_vouch_for(self) -> None:
        package, entry = self.package("good", self.distribution.vouched)
        out = self.verify(self.keyring(*self.distribution.main), self.repository(entry), package)
        self.assertEqual((out / f"pkg--{package.name}").read_bytes(), package.read_bytes())

    def test_rejects_a_packager_only_two_main_keys_vouch_for(self) -> None:
        package, entry = self.package("weak", self.distribution.weak)
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(*self.distribution.main), self.repository(entry), package)
        self.assertIn("do not vouch for", str(failure.exception))

    def test_only_declared_main_keys_vouch(self) -> None:
        # The third certification is from a main key the catalog did not declare, so it does not count
        # however much the keyring package trusts it.
        package, entry = self.package("undeclared", self.distribution.vouched)
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(*self.distribution.main[:2]), self.repository(entry), package)
        self.assertIn("do not vouch for", str(failure.exception))

    def test_rejects_a_key_nobody_vouches_for(self) -> None:
        package, entry = self.package("stray", self.distribution.stray)
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(*self.distribution.main), self.repository(entry), package)
        self.assertIn("do not vouch for", str(failure.exception))

    def test_rejects_a_packager_the_package_withdrew(self) -> None:
        package, entry = self.package("withdrawn", self.distribution.vouched)
        keyring = self.keyring(*self.distribution.main, revoked=(self.distribution.vouched,))
        with self.assertRaises(SystemExit) as failure:
            self.verify(keyring, self.repository(entry), package)
        self.assertIn("NO_PUBKEY", str(failure.exception))

    def test_rejects_a_packager_who_revoked_their_key(self) -> None:
        package, entry = self.package("revoked", self.distribution.vouched)
        keyring = self.keyring(*self.distribution.main, revocations=(self.distribution.vouched,))
        with self.assertRaises(SystemExit) as failure:
            self.verify(keyring, self.repository(entry), package)
        self.assertIn("REVKEYSIG", str(failure.exception))

    def test_judges_expiry_as_of_the_pinned_snapshot(self) -> None:
        # The key was good when the snapshot was published, and stays so for a build of that
        # snapshot however much later; a snapshot from after its expiry rejects it.
        package, entry = self.package("stale", self.distribution.stale)
        out = self.verify(self.keyring(*self.distribution.main), self.repository(entry), package)
        self.assertTrue((out / f"pkg--{package.name}").exists())
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(*self.distribution.main, time=LATER), self.repository(entry), package)
        self.assertIn("EXPKEYSIG", str(failure.exception))

    def test_rejects_a_tampered_package(self) -> None:
        package, entry = self.package("tampered", self.distribution.vouched)
        package.write_bytes(b"something else")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(*self.distribution.main), self.repository(entry), package)
        self.assertIn("BADSIG", str(failure.exception))

    def test_rejects_a_second_signature_riding_along(self) -> None:
        # A good signature from a vouched-for key followed by one from a stray key: read as one bag of
        # status words that would pass, so the reading demands one signature.
        package, entry = self.package("twice", self.distribution.stray)
        vouched = base64.b64decode(self.distribution.sign(self.distribution.vouched, package))
        stray = base64.b64decode(entry["PGPSIG"][0])
        entry["PGPSIG"] = [base64.b64encode(vouched + stray).decode()]
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(*self.distribution.main), self.repository(entry), package)
        self.assertIn("carries 2 signatures", str(failure.exception))

    def test_rejects_a_package_without_a_signature(self) -> None:
        package, entry = self.package("unsigned", None)
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(*self.distribution.main), self.repository(entry), package)
        self.assertIn("carries no signature", str(failure.exception))

    def test_rejects_a_signature_that_does_not_decode(self) -> None:
        package, entry = self.package("garbled", None)
        entry["PGPSIG"] = ["not base64!"]
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(*self.distribution.main), self.repository(entry), package)
        self.assertIn("undecodable", str(failure.exception))

    def test_rejects_a_pool_file_not_named_by_checksum(self) -> None:
        package, entry = self.package("misnamed", self.distribution.vouched)
        misnamed = package.with_name("misnamed-1-1-any.pkg.tar.zst")
        misnamed.write_bytes(package.read_bytes())
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(*self.distribution.main), self.repository(entry), misnamed)
        self.assertIn("not named by its checksum", str(failure.exception))

    def test_a_batch_names_the_rejected_package_only(self) -> None:
        good, good_entry = self.package("batch-good", self.distribution.vouched)
        bad, bad_entry = self.package("batch-bad", self.distribution.weak)
        keyring = self.keyring(*self.distribution.main)
        with self.assertRaises(SystemExit) as failure:
            self.verify(keyring, self.repository(good_entry, bad_entry), good, bad)
        self.assertIn(bad.name, str(failure.exception))
        self.assertNotIn(good.name, str(failure.exception))
        self.assertFalse((keyring.parent / "verified" / f"pkg--{good.name}").exists())


if __name__ == "__main__":
    unittest.main()
