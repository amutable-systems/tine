"""The certificate chain: what it accepts, and every way an operator can try to get past it.

The bucket is untrusted, so both the leaf certificate and the object it signed for arrive from an
attacker-controlled store.
"""

import base64
import datetime
import tempfile
import unittest
from pathlib import Path
from typing import override

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import signing
from test_ca import DAY, Signing, authority, issue, name, write_key

ACTION = "a" * 64
PAYLOAD = b"a bundle name and a result"


def load_der(pem: str) -> bytes:
    return signing.load_certificate(pem).public_bytes(serialization.Encoding.DER)


def to_pem(der: bytes) -> str:
    body = base64.encodebytes(der).decode()
    return f"-----BEGIN CERTIFICATE-----\n{body}-----END CERTIFICATE-----\n"


class ChainCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.ca_key, self.ca = authority("tine cache CA")
        self.leaf_key = Ed25519PrivateKey.generate()
        self.signer = self.a_signer(self.leaf_key, "leaf")
        self.leaf = self.issue_for(self.leaf_key)
        self.store: dict[str, bytes] = {signing.certificate_key(self.signer.id): self.leaf.encode()}
        self.authority = signing.Authority([self.ca], self.store.get)

    def a_signer(self, key: Signing, named: str) -> signing.Signer:
        return signing.Signer(write_key(self.root / named, key))

    def check(self, pem: str, named: bytes) -> None:
        self.authority.check(signing.load_certificate(pem), named)

    def issue_for(
        self,
        key: Signing,
        *,
        ca: bool = False,
        valid_from: datetime.timedelta = -DAY,
        valid_to: datetime.timedelta = 365 * DAY,
        digital_signature: bool = True,
    ) -> str:
        return issue(
            "builder",
            key,
            name("tine cache CA"),
            self.ca_key,
            ca=ca,
            valid_from=valid_from,
            valid_to=valid_to,
            digital_signature=digital_signature,
        )


class TestChain(ChainCase):
    def test_a_result_signed_by_a_certified_key_comes_back(self) -> None:
        """The certificate is looked for at `keys/<key id>`, which is where the builder puts it."""
        self.assertEqual(signing.certificate_key(self.signer.id), f"keys/{self.signer.id.hex()}")
        wrapped = self.signer.wrap(ACTION, PAYLOAD)
        self.assertEqual(self.authority.unwrap(ACTION, wrapped), PAYLOAD)

    def test_the_certificate_is_fetched_once_and_remembered(self) -> None:
        """One chain validation per unknown key, never one per lookup."""
        asked: list[str] = []

        def counting(key: str) -> bytes | None:
            asked.append(key)
            return self.store.get(key)

        authority = signing.Authority([self.ca], counting)
        for _ in range(5):
            authority.unwrap(ACTION, self.signer.wrap(ACTION, PAYLOAD))
        self.assertEqual(len(asked), 1)

    def test_a_leaf_the_store_does_not_have_is_a_refusal_not_a_crash(self) -> None:
        authority = signing.Authority([self.ca], lambda _: None)
        with self.assertRaisesRegex(ValueError, "not trusted here"):
            authority.unwrap(ACTION, self.signer.wrap(ACTION, PAYLOAD))

    def test_rotation_needs_nothing_from_a_reader(self) -> None:
        """The point of the indirection: a new leaf, the same trusted material everywhere."""
        replacement = Ed25519PrivateKey.generate()
        signer = self.a_signer(replacement, "next")
        self.store[signing.certificate_key(signer.id)] = self.issue_for(replacement).encode()
        self.assertEqual(self.authority.unwrap(ACTION, signer.wrap(ACTION, PAYLOAD)), PAYLOAD)


class TestRefusals(ChainCase):
    def test_a_leaf_signed_by_someone_else_is_refused(self) -> None:
        """The operator makes their own CA and their own leaf. This is the attack."""
        stranger_ca_key, _ = authority("tine cache CA")
        forged = issue("builder", self.leaf_key, name("tine cache CA"), stranger_ca_key)
        with self.assertRaisesRegex(ValueError, "candidates exhausted"):
            self.check(forged, self.signer.id)

    def test_a_leaf_that_is_itself_a_ca_is_refused(self) -> None:
        """Otherwise a signing key could mint more of them."""
        too_much = self.issue_for(self.leaf_key, ca=True)
        with self.assertRaisesRegex(ValueError, "is a CA"):
            self.check(too_much, self.signer.id)

    def test_a_leaf_not_marked_for_signing_is_refused(self) -> None:
        wrong_use = self.issue_for(self.leaf_key, digital_signature=False)
        with self.assertRaisesRegex(ValueError, "for signing"):
            self.check(wrong_use, self.signer.id)

    def test_a_leaf_that_says_nothing_about_itself_is_refused(self) -> None:
        """What `openssl x509 -req` makes without an extensions file: no CA flag, no key usage."""
        bare = issue("builder", self.leaf_key, name("tine cache CA"), self.ca_key, extensions=False)
        with self.assertRaisesRegex(ValueError, "missing required extension"):
            self.check(bare, self.signer.id)

    def test_a_certificate_with_an_impossible_version_is_refused_not_a_crash(self) -> None:
        """A one-byte edit of a real certificate raises cryptography's own class, not a ValueError."""
        der = bytearray(load_der(self.leaf))
        at = der.find(bytes([0xA0, 0x03, 0x02, 0x01, 0x02]))
        self.assertGreater(at, 0)
        der[at + 4] = 7
        with self.assertRaisesRegex(ValueError, "not a usable certificate"):
            signing.load_certificate(to_pem(bytes(der)))

    def test_a_certificate_filed_under_the_wrong_name_is_refused(self) -> None:
        """A store answering for the wrong key is broken, and working around it hides that."""
        other = Ed25519PrivateKey.generate()
        with self.assertRaisesRegex(ValueError, "holds key"):
            self.check(self.issue_for(other), self.signer.id)

    def test_a_certificate_that_is_not_a_certificate_is_refused(self) -> None:
        for material in (b"", b"nonsense", self.ca.encode()[:80]):
            self.store[signing.certificate_key(self.signer.id)] = material
            authority = signing.Authority([self.ca], self.store.get)
            with self.subTest(material=material[:16]), self.assertRaises(ValueError):
                authority.unwrap(ACTION, self.signer.wrap(ACTION, PAYLOAD))


class TestClock(ChainCase):
    """A shim outlives the builds that started it, so "valid" is a question with a time in it."""

    def moving(self, offset: datetime.timedelta) -> signing.Authority:
        return signing.Authority(
            [self.ca], self.store.get, clock=lambda: datetime.datetime.now(datetime.UTC) + offset
        )

    def test_a_leaf_that_expires_while_we_run_is_refused_until_renewed(self) -> None:
        """The check that bounds a stolen key is worth nothing if it is only made once.

        And a renewal keeps the key and so the name, so the remembered certificate is the stale one
        and the bucket has to be asked again before the result is refused for good.
        """
        self.store[signing.certificate_key(self.signer.id)] = self.issue_for(
            self.leaf_key, valid_to=2 * DAY
        ).encode()
        authority = self.moving(datetime.timedelta())
        wrapped = self.signer.wrap(ACTION, PAYLOAD)
        self.assertEqual(authority.unwrap(ACTION, wrapped), PAYLOAD)

        authority.clock = lambda: datetime.datetime.now(datetime.UTC) + 3 * DAY
        with self.assertRaisesRegex(ValueError, "not valid at validation time"):
            authority.unwrap(ACTION, wrapped)
        self.store[signing.certificate_key(self.signer.id)] = self.issue_for(
            self.leaf_key, valid_to=30 * DAY
        ).encode()
        self.assertEqual(authority.unwrap(ACTION, wrapped), PAYLOAD)

    def test_an_authority_that_expires_while_we_run_takes_its_leaves_with_it(self) -> None:
        """A leaf outliving its issuer: the chain, not the leaf, is what runs out."""
        self.store[signing.certificate_key(self.signer.id)] = self.issue_for(
            self.leaf_key, valid_to=500 * DAY
        ).encode()
        authority = self.moving(datetime.timedelta())
        wrapped = self.signer.wrap(ACTION, PAYLOAD)
        self.assertEqual(authority.unwrap(ACTION, wrapped), PAYLOAD)

        authority.clock = lambda: datetime.datetime.now(datetime.UTC) + 400 * DAY
        with self.assertRaisesRegex(ValueError, "candidates exhausted: cert is not valid"):
            authority.unwrap(ACTION, wrapped)

    def test_a_leaf_not_yet_valid_becomes_valid_without_a_restart(self) -> None:
        """The same rule read the other way, and what a clock skew at rotation looks like."""
        self.store[signing.certificate_key(self.signer.id)] = self.issue_for(
            self.leaf_key, valid_from=DAY, valid_to=10 * DAY
        ).encode()
        authority = self.moving(datetime.timedelta())
        with self.assertRaisesRegex(ValueError, "not valid at validation time"):
            authority.unwrap(ACTION, self.signer.wrap(ACTION, PAYLOAD))

        authority.clock = lambda: datetime.datetime.now(datetime.UTC) + 2 * DAY
        self.assertEqual(authority.unwrap(ACTION, self.signer.wrap(ACTION, PAYLOAD)), PAYLOAD)


class TestAuthorities(ChainCase):
    def test_an_authority_that_is_not_a_ca_is_refused_at_startup(self) -> None:
        """A configuration mistake, and one worth catching before a build depends on it."""
        not_a_ca = self.issue_for(self.leaf_key)
        with self.assertRaisesRegex(ValueError, "not marked as a CA"):
            signing.Authority([not_a_ca], self.store.get)

    def test_an_expired_authority_is_dropped_not_fatal(self) -> None:
        """The old CA stays configured for a while after a rotation, and one day it expires."""
        key, _ = authority()
        old = issue("old CA", key, name("old CA"), key, ca=True, valid_from=-10 * DAY, valid_to=-DAY)
        with self.assertLogs("signing", "WARNING") as logged:
            both = signing.Authority([old, self.ca], self.store.get)
        self.assertIn("expired on", logged.output[0])
        self.assertEqual(both.unwrap(ACTION, self.signer.wrap(ACTION, PAYLOAD)), PAYLOAD)
        with self.assertRaisesRegex(ValueError, "would trust nothing"):
            signing.Authority([old], self.store.get)

    def test_no_authority_at_all_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "would trust nothing"):
            signing.Authority([], self.store.get)

    def test_several_authorities_are_allowed(self) -> None:
        """Rotating the CA means both being trusted for a while."""
        _, other = authority("next CA")
        both = signing.Authority([other, self.ca], self.store.get)
        self.assertEqual(both.unwrap(ACTION, self.signer.wrap(ACTION, PAYLOAD)), PAYLOAD)

    def test_a_builder_stops_signing_before_its_results_would_outlive_the_certificate(self) -> None:
        """A reader wants the certificate valid when it reads, and a pointer is read until it expires."""
        certificate = self.root / "short.crt"
        certificate.write_text(self.issue_for(self.leaf_key, valid_to=2 * DAY))
        signing.Signer(self.root / "leaf", certificate, object_lifetime=DAY).wrap(ACTION, PAYLOAD)
        with self.assertRaisesRegex(ValueError, "time to rotate"):
            signing.Signer(self.root / "leaf", certificate, object_lifetime=3 * DAY).wrap(ACTION, PAYLOAD)
        certificate.write_text(self.issue_for(self.leaf_key, valid_from=-3 * DAY, valid_to=-DAY))
        with self.assertRaisesRegex(ValueError, "signing certificate expired"):
            signing.Signer(self.root / "leaf", certificate).wrap(ACTION, PAYLOAD)

    def test_a_builder_learns_at_startup_whether_anyone_would_read_it(self) -> None:
        """Judged on the certificate in hand, so nothing is published before it passes."""
        certificate = self.root / "leaf.crt"
        certificate.write_text(self.leaf)
        self.authority.admit(signing.Signer(self.root / "leaf", certificate))
        with self.assertRaisesRegex(ValueError, "no certificate"):
            self.authority.admit(self.signer)
        other = Ed25519PrivateKey.generate()
        self.a_signer(other, "unknown")
        with self.assertRaisesRegex(ValueError, "not a certificate for signing key"):
            signing.Signer(self.root / "unknown", certificate)
        certificate.write_text(issue("stranger", other, name("elsewhere"), other))
        with self.assertRaisesRegex(ValueError, "candidates exhausted"):
            self.authority.admit(signing.Signer(self.root / "unknown", certificate))
