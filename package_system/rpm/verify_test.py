# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the signature verifier against the real `rpmkeys` binary

Uses local (mock) GPG keys and packages.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

import keyring
import verify

SPEC = """\
Name: pkg
Version: 1
Release: 1
Summary: A package to sign
License: MIT
BuildArch: noarch
%description
%files
"""


def _run(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


class Key:
    """An OpenPGP key in its own gpg home, with the armored public part as a file named by fingerprint."""

    def __init__(self, home: Path, name: str) -> None:
        self.home = home
        self.name = name
        home.mkdir(mode=0o700)
        self.gpg("--quick-generate-key", f"{name} <{name}@tine.test>", "rsa2048", "sign", "never")
        colons = self.gpg("--list-keys", "--with-colons")
        self.fingerprint = next(
            line.split(":")[9] for line in colons.splitlines() if line.startswith("fpr:")
        )
        self.file = home / f"{self.fingerprint}.key"
        self.file.write_text(self.gpg("--export", "--armor"))

    def gpg(self, *args: str) -> str:
        return _run(
            "gpg",
            "--batch",
            "--homedir",
            str(self.home),
            "--passphrase",
            "",
            "--pinentry-mode",
            "loopback",
            *args,
        )

    def sign(self, package: Path, out: Path) -> None:
        """A copy of the package signed by this key, through rpm's own signing tool."""
        out.write_bytes(package.read_bytes())
        _run(
            "rpmsign",
            "--addsign",
            "--define",
            f"_gpg_name {self.name}",
            "--define",
            f"_gpg_path {self.home}",
            str(out),
        )


class TestVerify(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls._scratch = tempfile.TemporaryDirectory()
        scratch = Path(cls._scratch.name)
        cls.signer = Key(scratch / "signer", "signer")
        cls.other = Key(scratch / "other", "other")
        cls.addClassCleanup(cls._kill_agents)

        (scratch / "pkg.spec").write_text(SPEC)
        _run(
            "rpmbuild",
            "-bb",
            "--quiet",
            "--define",
            f"_topdir {scratch}/rpmbuild",
            "--define",
            "_buildhost tine",
            str(scratch / "pkg.spec"),
        )
        cls.unsigned = scratch / "rpmbuild/RPMS/noarch/pkg-1-1.noarch.rpm"
        cls.signed = scratch / "pkg-1-1.noarch.signed.rpm"
        cls.signer.sign(cls.unsigned, cls.signed)

    @classmethod
    def _kill_agents(cls) -> None:
        """Calling gpg autolaunches an agent."""

        for key in (cls.signer, cls.other):
            _run("gpgconf", "--homedir", str(key.home), "--kill", "all")

    def keyring(self, *keys: Key, declared: dict[str, Path] | None = None) -> Path:
        out = Path(tempfile.mkdtemp(dir=self._scratch.name)) / "keyring"
        files = declared if declared is not None else {key.fingerprint: key.file for key in keys}
        keyring.keyring(
            keyring.Spec(keys={f: str(path) for f, path in files.items()}, out=str(out), time=None)
        )
        return out

    def verify(self, keyring: Path, *packages: Path) -> Path:
        out = keyring.parent / "verified"
        verify.verify(
            verify.Spec(
                keyring=str(keyring),
                out=str(out),
                packages={p.name: str(p) for p in packages},
                repository=str(keyring.parent),
            )
        )
        return out

    def test_keyring_names_the_declared_keys_the_way_rpm_does(self) -> None:
        # What the fingerprint check parses, so a change in rpm's naming fails here first.
        keyring = self.keyring(self.signer, self.other)
        self.assertEqual(
            sorted(path.name for path in keyring.iterdir()),
            sorted(f"gpg-pubkey-{key.fingerprint.lower()}.key" for key in (self.signer, self.other)),
        )

    def test_keyring_refuses_a_key_file_under_another_fingerprint(self) -> None:
        with self.assertRaises(SystemExit) as failure:
            self.keyring(declared={self.other.fingerprint: self.signer.file})
        self.assertIn(f"absent ['{self.other.fingerprint}']", str(failure.exception))
        self.assertIn(f"undeclared ['{self.signer.fingerprint}']", str(failure.exception))

    def test_keyring_refuses_a_second_key_riding_along_in_a_declared_file(self) -> None:
        bundle = Path(self._scratch.name) / "bundle.key"
        bundle.write_text(self.signer.file.read_text() + self.other.file.read_text())
        with self.assertRaises(SystemExit) as failure:
            self.keyring(declared={self.signer.fingerprint: bundle})
        self.assertIn(f"undeclared ['{self.other.fingerprint}']", str(failure.exception))

    def test_publishes_the_packages_its_keys_signed(self) -> None:
        other_signed = Path(self._scratch.name) / "pkg-1-1.noarch.other.rpm"
        self.other.sign(self.unsigned, other_signed)
        out = self.verify(self.keyring(self.signer, self.other), self.signed, other_signed)
        for package in (self.signed, other_signed):
            self.assertEqual((out / package.name).read_bytes(), package.read_bytes())

    def test_rejects_a_package_another_key_signed(self) -> None:
        keyring = self.keyring(self.other)
        with self.assertRaises(SystemExit) as failure:
            self.verify(keyring, self.signed)
        self.assertIn(
            f"no valid signature from the declared keys on {self.signed.name}", str(failure.exception)
        )
        self.assertFalse((keyring.parent / "verified" / self.signed.name).exists())

    def test_rejects_an_unsigned_package(self) -> None:
        # Well formed, so what fails below is the missing signature and nothing else.
        _run("rpmkeys", "--define", "_pkgverify_level digest", "--checksig", str(self.unsigned))
        with self.assertRaises(SystemExit):
            self.verify(self.keyring(self.signer), self.unsigned)

    def test_a_batch_names_the_rejected_package_only(self) -> None:
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.keyring(self.signer), self.signed, self.unsigned)
        self.assertIn(f"on {self.unsigned.name},", str(failure.exception))
        self.assertNotIn(self.signed.name, str(failure.exception))


if __name__ == "__main__":
    unittest.main()
