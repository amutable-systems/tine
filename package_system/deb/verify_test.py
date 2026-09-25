# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the Release verifier against the real `sqv` binary.

A throwaway archive: gpg makes its keys and signs its Release, the way any test can, and sqv,
which the driver runs, verifies. It lives at a fixed date, the way a pinned snapshot does, so
what expires and when is under the test's control.
"""

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

import openpgp
import release

import keyring
import snapshotter
import verify

# The archive's keys are made on the first day and its Release signed on the second; the
# snapshot the tests verify against is pinned to the third.
MADE = "20260101T000000"
SIGNED = "20260102T000000"
BEFORE_SIGNING = "2026-01-01T12:00:00Z"
PINNED = "2026-01-03T00:00:00Z"
LATER_SIGNING = "20260115T000000"
LATER = "2026-02-01T00:00:00Z"
# What the Release says about itself: published the day it was signed, current for a week after.
DATED = "Fri, 02 Jan 2026 00:00:00 UTC"
VALID_UNTIL = "Fri, 09 Jan 2026 00:00:00 UTC"


class Archive:
    """An archive's keys in one gpg home."""

    def __init__(self, home: Path) -> None:
        self.home = home
        home.mkdir(mode=0o700)
        self.key = self._generate("archive")
        self.previous = self._generate("previous")
        self.stray = self._generate("stray")
        self.stale = self._generate("stale", expire="7d")

    def gpg(self, *args: str, at: str = MADE, stdin: str | None = None) -> str:
        command = [
            "gpg",
            "--homedir",
            str(self.home),
            "--batch",
            "--no-tty",
            "--faked-system-time",
            at,
            "--quiet",
        ]
        return subprocess.run(
            [*command, *args], input=stdin, capture_output=True, check=True, text=True
        ).stdout

    def _generate(self, name: str, expire: str = "never") -> str:
        self.gpg(
            "--passphrase", "", "--pinentry-mode", "loopback",
            "--quick-generate-key", f"{name} <{name}@tine.test>", "ed25519", "sign", expire,
        )  # fmt: skip
        listing = self.gpg("--with-colons", "--list-keys", f"{name}@tine.test")
        return next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")).upper()

    def key_file(self, fingerprint: str, directory: Path, secret: bool = False) -> Path:
        path = directory / f"{fingerprint}.key"
        export = "--export-secret-keys" if secret else "--export"
        path.write_text(
            self.gpg("--passphrase", "", "--pinentry-mode", "loopback", export, "--armor", fingerprint)
        )
        return path

    def clearsign(self, text: str, *signers: str, at: str = SIGNED) -> bytes:
        """The text as the archive publishes it, signed by each signer in turn."""
        users = [argument for signer in signers for argument in ("--local-user", signer)]
        return self.gpg(*users, "--clearsign", stdin=text, at=at).encode()


class TestVerify(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls._scratch = tempfile.TemporaryDirectory()
        scratch = Path(cls._scratch.name)
        cls.archive = Archive(scratch / "archive")
        cls.addClassCleanup(
            subprocess.run, ["gpgconf", "--homedir", str(cls.archive.home), "--kill", "all"], check=True
        )
        cls.pool = scratch / "pool"
        cls.pool.mkdir()

    def package(self, name: str, content: bytes | None = None) -> tuple[Path, str]:
        """A pool artifact named by checksum, and the index record describing it."""
        content = content if content is not None else f"{name} contents".encode()
        checksum = hashlib.sha256(content).hexdigest()
        path = self.pool / f"{checksum}.deb"
        path.write_bytes(content)
        record = (
            f"Package: {name}\nVersion: 1\nArchitecture: amd64\n"
            f"Filename: pool/main/{name}_1_amd64.deb\nSize: {len(content)}\nSHA256: {checksum}\n\n"
        )
        return path, record

    def repository(
        self,
        *records: str,
        signers: tuple[str, ...] | None = None,
        date: str = DATED,
        valid_until: str | None = None,
        stated_at: str = "main/binary-amd64",
    ) -> Path:
        """A pinned repository: the index the records make up, and a Release signed over it."""
        directory = Path(tempfile.mkdtemp(dir=self._scratch.name)) / "repo"
        directory.mkdir()
        index = "".join(records).encode()
        (directory / "Packages").write_bytes(index)
        expires = f"Valid-Until: {valid_until}\n" if valid_until is not None else ""
        text = (
            f"Origin: tine\nSuite: testing\nDate: {date}\n{expires}"
            "Architectures: amd64\nComponents: main\nSHA256:\n"
            f" {hashlib.sha256(index).hexdigest()} {len(index)} {stated_at}/Packages\n"
        )
        signers = signers if signers is not None else (self.archive.key,)
        signed = self.archive.clearsign(text, *signers) if signers else text.encode()
        (directory / release.INRELEASE).write_bytes(signed)
        return directory

    def retain(self, repository: Path, older: Path, pinned_at: str | None = None) -> None:
        """Retain `older`'s generation under `repository`, as a committed lock has it materialized."""
        retained = repository / snapshotter.RETAINED
        retained.mkdir(exist_ok=True)
        generation = retained / str(len(list(retained.iterdir())))
        older.rename(generation)
        if pinned_at is not None:
            (generation / snapshotter.PINNED_AT).write_text(pinned_at + "\n")

    def keyring(
        self, *declared: str, files: dict[str, Path] | None = None, time: str | None = PINNED
    ) -> Path:
        scratch = Path(tempfile.mkdtemp(dir=self._scratch.name))
        if files is None:
            files = {f: self.archive.key_file(f, scratch) for f in declared}
        out = scratch / "keyring"
        keyring.keyring(
            keyring.Spec(keys={f: str(path) for f, path in files.items()}, out=str(out), time=time)
        )
        return out

    def verify(self, keyring: Path, repository: Path, *packages: Path) -> Path:
        out = keyring.parent / "verified"
        verify.verify(
            verify.Spec(
                arch="amd64",
                component="main",
                keyring=str(keyring),
                out=str(out),
                packages={f"pkg--{p.name}": str(p) for p in packages},
                repository=str(repository),
            )
        )
        return out

    def test_keyring_holds_the_declared_certificates_and_the_pinned_time(self) -> None:
        keyring = self.keyring(self.archive.key, self.archive.previous)
        self.assertEqual(
            sorted(path.name for path in keyring.iterdir()),
            sorted((openpgp.KEYRING_FILE, openpgp.TIME_FILE)),
        )
        found = openpgp.primary_fingerprints((keyring / openpgp.KEYRING_FILE).read_bytes())
        self.assertEqual(found, sorted((self.archive.key, self.archive.previous)))
        self.assertEqual((keyring / openpgp.TIME_FILE).read_text(), PINNED + "\n")
        self.assertFalse((self.keyring(self.archive.key, time=None) / openpgp.TIME_FILE).exists())

    def test_keyring_refuses_a_key_file_under_another_fingerprint(self) -> None:
        scratch = Path(tempfile.mkdtemp(dir=self._scratch.name))
        with self.assertRaises(SystemExit) as failure:
            self.keyring(files={self.archive.previous: self.archive.key_file(self.archive.key, scratch)})
        self.assertIn(
            f"holds ['{self.archive.key}'], not the declared key {self.archive.previous}",
            str(failure.exception),
        )

    def test_keyring_refuses_a_key_file_that_is_no_public_key(self) -> None:
        scratch = Path(tempfile.mkdtemp(dir=self._scratch.name))
        (scratch / "junk.key").write_text("<html>Access Denied</html>\n")
        for what, path in (
            ("junk", scratch / "junk.key"),
            ("secret", self.archive.key_file(self.archive.key, scratch, secret=True)),
        ):
            with self.subTest(what=what), self.assertRaises(SystemExit) as failure:
                self.keyring(files={self.archive.key: path})
            self.assertIn("is not a public key file", str(failure.exception))

    def test_keyring_refuses_a_time_it_cannot_read(self) -> None:
        with self.assertRaises(SystemExit) as failure:
            self.keyring(self.archive.key, time="yesterday")
        self.assertIn("not an ISO 8601 time", str(failure.exception))

    def test_publishes_a_package_the_signed_release_vouches_for(self) -> None:
        package, record = self.package("good")
        out = self.verify(self.keyring(self.archive.key), self.repository(record), package)
        self.assertEqual((out / f"pkg--{package.name}").read_bytes(), package.read_bytes())

    def test_one_declared_key_among_the_signers_suffices(self) -> None:
        # The archive signs with its current key and the one before; a keyring declaring either
        # verifies, and a stray signature riding along changes nothing.
        package, record = self.package("either")
        signers = (self.archive.stray, self.archive.key, self.archive.previous)
        for declared in (self.archive.key, self.archive.previous):
            with self.subTest(declared=declared):
                out = self.verify(self.keyring(declared), self.repository(record, signers=signers), package)
                self.assertTrue((out / f"pkg--{package.name}").exists())

    def test_rejects_a_release_no_declared_key_signed(self) -> None:
        package, record = self.package("stray")
        for signers in ((self.archive.stray,), ()):
            with self.subTest(signers=signers), self.assertRaises(SystemExit) as failure:
                self.verify(
                    self.keyring(self.archive.key), self.repository(record, signers=signers), package
                )
            self.assertIn("no valid signature from the declared keys", str(failure.exception))

    def test_rejects_a_tampered_release(self) -> None:
        package, record = self.package("tampered-release")
        repository = self.repository(record)
        signed = repository / release.INRELEASE
        signed.write_bytes(signed.read_bytes().replace(b"Suite: testing", b"Suite: unstable"))
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(self.archive.key), repository, package)
        self.assertIn("no valid signature from the declared keys", str(failure.exception))

    def test_rejects_an_index_the_release_does_not_state(self) -> None:
        package, record = self.package("unstated")
        _, extra = self.package("extra")
        repository = self.repository(record)
        (repository / "Packages").write_text(record + extra)
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(self.archive.key), repository, package)
        self.assertIn("states no main/binary-amd64/Packages", str(failure.exception))

    def test_rejects_an_index_the_release_states_for_another_component(self) -> None:
        package, record = self.package("elsewhere")
        repository = self.repository(record, stated_at="contrib/binary-amd64")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(self.archive.key), repository, package)
        self.assertIn("states no main/binary-amd64/Packages", str(failure.exception))

    def test_rejects_a_release_published_after_the_pinned_snapshot(self) -> None:
        package, record = self.package("ahead")
        repository = self.repository(record, date="Sun, 01 Feb 2026 00:00:00 UTC")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(self.archive.key), repository, package)
        self.assertIn("after the", str(failure.exception))

    def test_rejects_a_release_that_had_expired_by_the_pinned_snapshot(self) -> None:
        package, record = self.package("stale")
        repository = self.repository(record, valid_until="Fri, 02 Jan 2026 12:00:00 UTC")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(self.archive.key), repository, package)
        self.assertIn("expired at", str(failure.exception))

    def test_a_release_still_current_at_the_pin_verifies(self) -> None:
        package, record = self.package("current")
        repository = self.repository(record, valid_until=VALID_UNTIL)
        out = self.verify(self.keyring(self.archive.key), repository, package)
        self.assertEqual([path.name for path in out.iterdir()], [f"pkg--{package.name}"])

    def test_rejects_a_release_that_states_no_date(self) -> None:
        package, record = self.package("undated")
        repository = self.repository(record, date="")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(self.archive.key), repository, package)
        self.assertIn("missing date", str(failure.exception).lower())

    def test_rejects_a_package_the_index_does_not_describe(self) -> None:
        package, _ = self.package("undescribed")
        _, record = self.package("described")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(self.archive.key), self.repository(record), package)
        self.assertIn("vouches for checksum", str(failure.exception))

    def test_rejects_a_tampered_package(self) -> None:
        package, record = self.package("tampered")
        package.write_bytes(b"something else")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(self.archive.key), self.repository(record), package)
        self.assertIn("not the checksum it is named by", str(failure.exception))

    def test_rejects_a_pool_file_not_named_by_checksum(self) -> None:
        package, record = self.package("misnamed")
        misnamed = package.with_name("misnamed_1_amd64.deb")
        misnamed.write_bytes(package.read_bytes())
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(self.archive.key), self.repository(record), misnamed)
        self.assertIn("not named by its checksum", str(failure.exception))

    def test_refuses_a_signature_from_after_the_pinned_snapshot(self) -> None:
        # The pin bounds when the Release may have been signed, so a snapshot verifies the same
        # way however much later it is built.
        package, record = self.package("early")
        repository = self.repository(record)
        out = self.verify(self.keyring(self.archive.key), repository, package)
        self.assertTrue((out / f"pkg--{package.name}").exists())
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(self.archive.key, time=BEFORE_SIGNING), repository, package)
        self.assertIn("no valid signature", str(failure.exception))

    def test_a_key_valid_when_it_signed_stays_so(self) -> None:
        # sqv judges a key as of the signature it made, as APT does on a box that verifies with
        # it: the key's expiry a week later invalidates nothing, pinned or not.
        package, record = self.package("stale")
        repository = self.repository(record, signers=(self.archive.stale,))
        for time in (PINNED, LATER, None):
            with self.subTest(time=time):
                out = self.verify(self.keyring(self.archive.stale, time=time), repository, package)
                self.assertTrue((out / f"pkg--{package.name}").exists())

    def test_publishes_a_package_only_a_retained_release_vouches_for(self) -> None:
        # The pin moved on and dropped the package; the lock that selected it retains the Release
        # it was resolved against, which still vouches for it.
        dropped, dropped_record = self.package("dropped")
        current, current_record = self.package("current")
        repository = self.repository(current_record)
        self.retain(repository, self.repository(dropped_record))
        out = self.verify(self.keyring(self.archive.key), repository, dropped, current)
        for package in (dropped, current):
            self.assertTrue((out / f"pkg--{package.name}").exists())

    def test_judges_a_retained_release_as_of_its_own_pin(self) -> None:
        # A lock resolved against a pin later than the repository's, which a rolled-back pin
        # leaves behind, retains a Release signed after the repository's pinned time.
        package, record = self.package("later")
        repository = self.repository()
        later = self.repository(record)
        signed = later / release.INRELEASE
        signed.write_bytes(
            self.archive.clearsign(
                release.unsigned("test", signed.read_bytes()).decode() + "\n",
                self.archive.key,
                at=LATER_SIGNING,
            )
        )
        self.retain(repository, later)
        keyring = self.keyring(self.archive.key)
        with self.assertRaises(SystemExit) as failure:
            self.verify(keyring, repository, package)
        self.assertIn("no valid signature", str(failure.exception))
        (repository / snapshotter.RETAINED / "0" / snapshotter.PINNED_AT).write_text(LATER + "\n")
        out = self.verify(self.keyring(self.archive.key), repository, package)
        self.assertTrue((out / f"pkg--{package.name}").exists())

    def test_rejects_a_retained_release_no_declared_key_signed(self) -> None:
        package, record = self.package("retained-stray")
        repository = self.repository()
        self.retain(repository, self.repository(record, signers=(self.archive.stray,)))
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(self.archive.key), repository, package)
        self.assertIn("no valid signature", str(failure.exception))

    def test_authenticates_a_retained_release_only_for_what_the_pinned_one_lacks(self) -> None:
        # What a lock retained is the concern of the packages needing it: a retained Release no
        # declared key signed rejects those and no other.
        current, current_record = self.package("current-only")
        dropped, dropped_record = self.package("dropped-unsigned")
        repository = self.repository(current_record)
        self.retain(repository, self.repository(dropped_record, signers=(self.archive.stray,)))
        out = self.verify(self.keyring(self.archive.key), repository, current)
        self.assertTrue((out / f"pkg--{current.name}").exists())
        with self.assertRaises(SystemExit):
            self.verify(self.keyring(self.archive.key), repository, current, dropped)

    def test_a_batch_names_the_rejected_package_only(self) -> None:
        good, good_record = self.package("batch-good")
        bad, _ = self.package("batch-bad")
        keyring = self.keyring(self.archive.key)
        with self.assertRaises(SystemExit) as failure:
            self.verify(keyring, self.repository(good_record), good, bad)
        self.assertIn(bad.name, str(failure.exception))
        self.assertNotIn(good.name, str(failure.exception))
        self.assertFalse((keyring.parent / "verified" / f"pkg--{good.name}").exists())


if __name__ == "__main__":
    unittest.main()
