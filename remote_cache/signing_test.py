# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The signature over an `ac/` object: what it accepts, and every way it says no.

The chain above the leaf is `authority_test`'s business; here the authority is a fixture and the
object is what gets attacked.
"""

import tempfile
import unittest
from pathlib import Path
from typing import override

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import signing
import test_ca

ACTION = "a" * 64
OTHER_ACTION = "b" * 64
PAYLOAD = b"c" * 64 + b"an action result, more or less"


class SigningCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.ca_key, self.ca = test_ca.authority("signing CA")
        self.store: dict[str, bytes] = {}
        # As `ssh-keygen -t ed25519` leaves it, which is how a key often arrives.
        self.signer = self.certified("builder", Ed25519PrivateKey.generate(), openssh=True)
        # A key nobody certified, which is what an attacker with bucket write access has.
        self.stranger = signing.Signer(test_ca.ed25519(self.root, "somebody-else")[0])
        self.verifier = signing.Authority([self.ca], self.store.get)

    def certified(self, named: str, key: test_ca.Signing, openssh: bool = False) -> signing.Signer:
        """A signer whose leaf certificate is where a reader will look for it."""
        signer = signing.Signer(test_ca.write_key(self.root / named, key, openssh))
        leaf = test_ca.issue("builder", key, test_ca.name("signing CA"), self.ca_key)
        self.store[signing.certificate_key(signer.id)] = leaf.encode()
        return signer


class TestRoundTrip(SigningCase):
    def test_a_signed_payload_comes_back_unchanged(self) -> None:
        wrapped = self.signer.wrap(ACTION, PAYLOAD)
        self.assertEqual(self.verifier.unwrap(ACTION, wrapped), PAYLOAD)

    def test_the_payload_is_stored_as_it_was_signed(self) -> None:
        """Signing covers the stored bytes, so the tail of the object is the payload itself."""
        wrapped = self.signer.wrap(ACTION, PAYLOAD)
        _, _, signature, payload = signing.split(wrapped)
        self.assertEqual(payload, PAYLOAD)
        self.assertEqual(len(wrapped), signing.HEADER.size + len(signature) + len(PAYLOAD))
        self.assertTrue(wrapped.endswith(PAYLOAD))

    def test_an_empty_payload_still_signs(self) -> None:
        self.assertEqual(self.verifier.unwrap(ACTION, self.signer.wrap(ACTION, b"")), b"")

    def test_the_key_id_is_what_openssl_would_show(self) -> None:
        """So a log line about an unknown key is something a person can look up.

        A fixed vector: the key below and the first 8 bytes of what
        `openssl pkey -pubin -outform DER < key.pub | sha256sum` printed for it.
        """
        pem = (
            "-----BEGIN PUBLIC KEY-----\n"
            "MCowBQYDK2VwAyEAGb9ECWmEzf6FQbrBZ9w7lshQhqowtrbLDFw4rXAxZuE=\n"
            "-----END PUBLIC KEY-----\n"
        )
        key = serialization.load_pem_public_key(pem.encode())
        self.assertEqual(signing.key_id(key).hex(), "a1e9156054e04fac")


class TestRefusals(SigningCase):
    def wrapped(self) -> bytes:
        return self.signer.wrap(ACTION, PAYLOAD)

    def test_an_object_under_another_action_is_refused(self) -> None:
        """Moving a valid result to another action digest is the attack the binding prevents."""
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.verifier.unwrap(OTHER_ACTION, self.wrapped())

    def test_the_action_hash_has_one_width(self) -> None:
        """Nothing separates hash from payload in the signed bytes, so a shorter hash may not exist."""
        for hash_ in (ACTION[:-2], ACTION + "aa", "z" * 64):
            with self.subTest(hash_=hash_), self.assertRaisesRegex(ValueError, "not a SHA-256"):
                self.signer.wrap(hash_, PAYLOAD)

    def test_a_changed_payload_is_refused(self) -> None:
        wrapped = bytearray(self.wrapped())
        wrapped[-1] ^= 0x01
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.verifier.unwrap(ACTION, bytes(wrapped))

    def test_a_changed_signature_is_refused(self) -> None:
        wrapped = bytearray(self.wrapped())
        wrapped[signing.HEADER.size] ^= 0x01
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.verifier.unwrap(ACTION, bytes(wrapped))

    def test_a_key_nobody_certified_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "not trusted here"):
            self.verifier.unwrap(ACTION, self.stranger.wrap(ACTION, PAYLOAD))

    def test_an_unsigned_object_is_refused(self) -> None:
        """What the bucket held before signing, and what an attacker would write to avoid it."""
        with self.assertRaisesRegex(ValueError, "not signed"):
            self.verifier.unwrap(ACTION, PAYLOAD + b"x" * signing.HEADER.size)

    def test_a_truncated_object_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "too short"):
            self.verifier.unwrap(ACTION, self.wrapped()[: signing.HEADER.size - 1])

    def test_an_unknown_version_or_algorithm_is_refused(self) -> None:
        """A newer writer's format has to be refused rather than read as this one."""
        for field in ("version", "algorithm"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                self.verifier.unwrap(ACTION, reheaded(self.wrapped(), **{field: 9}))

    def test_a_key_id_naming_another_key_is_refused(self) -> None:
        """Claiming a trusted key's name does not make the signature verify under it."""
        wrapped = reheaded(self.stranger.wrap(ACTION, PAYLOAD), named=self.signer.id)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.verifier.unwrap(ACTION, wrapped)


class TestAlgorithms(SigningCase):
    """The leaf is a file and can always be Ed25519, so it is; the authority is the TPM's problem."""

    def test_a_leaf_that_is_not_ed25519_is_refused(self) -> None:
        p256 = ec.generate_private_key(ec.SECP256R1())
        with self.assertRaisesRegex(ValueError, "a signing key is Ed25519"):
            self.certified("p256", p256)
        self.store[signing.certificate_key(self.signer.id)] = test_ca.issue(
            "builder", p256, test_ca.name("signing CA"), self.ca_key
        ).encode()
        with self.assertRaisesRegex(ValueError, "not Ed25519"):
            signing.Authority([self.ca], self.store.get).unwrap(ACTION, self.signer.wrap(ACTION, PAYLOAD))

    def test_a_private_key_in_pkcs8_pem_is_taken_too(self) -> None:
        """`openssl genpkey` writes this; `ssh-keygen` writes the other. Both turn up."""
        signer = self.certified("pkcs8", Ed25519PrivateKey.generate())
        self.assertEqual(self.verifier.unwrap(ACTION, signer.wrap(ACTION, PAYLOAD)), PAYLOAD)


def reheaded(wrapped: bytes, **changes: object) -> bytes:
    """The same object with header fields replaced, which is what a forger edits."""
    names = ("magic", "version", "algorithm", "named", "length")
    fields = dict(zip(names, signing.HEADER.unpack_from(wrapped), strict=True))
    fields.update(changes)
    return signing.HEADER.pack(*fields.values()) + wrapped[signing.HEADER.size :]


class TestFraming(SigningCase):
    def test_a_moved_signature_boundary_is_refused(self) -> None:
        """Past the end is never a short read of what followed; short feeds signature bytes in."""
        wrapped = self.signer.wrap(ACTION, PAYLOAD)
        for length, why in ((0xFFFF, "are there"), (32, "does not match")):
            with self.subTest(length=length), self.assertRaisesRegex(ValueError, why):
                self.verifier.unwrap(ACTION, reheaded(wrapped, length=length))
