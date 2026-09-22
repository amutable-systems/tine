# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Attest who wrote an `ac/` object.

Every other object in the bucket is named by a hash of its own content, so a digest is enough to catch a
change. An `ac/` object is not: it is keyed by an action digest, and nothing in its bytes relates to that
key. Anyone who can write to the bucket can put attacker-chosen outputs under an action a build is about
to look up ("cache poisoning"). To prevent that, their stored bytes get signed.

Keys are PEM, which is what the infrastructure that will hold them already produces: an X.509
certificate for a key the build server's TPM signed for. A key is named by the first bytes of the
SHA-256 of its DER SubjectPublicKeyInfo (what `openssl pkey -pubin -outform DER | sha256sum` prints), so
an unknown name in a log can be looked up. Name collisions are harmless beyond that logging.
"""

import datetime
import hashlib
import logging
import struct
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol, cast

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.types import PublicKeyTypes
from cryptography.x509.verification import (
    Criticality,
    ExtensionPolicy,
    Policy,
    PolicyBuilder,
    Store,
    VerificationError,
)

log = logging.getLogger("signing")

MAGIC = b"TINE"
VERSION = 1

# What signed an `ac/` object. The leaf is a file and can always be the fast thing, so there is one
# algorithm; the certificate says what signed the certificate, and that is x509's business, not this
# envelope's.
ED25519 = 1

KEY_ID_LENGTH = 8

# Magic, version, algorithm, key id, signature length, big-endian. An Ed25519 signature is always 64
# bytes; the length is written down anyway so that a second algorithm with a variable one would be a
# constant, not a format change.
HEADER = struct.Struct(f">{len(MAGIC)}sBB{KEY_ID_LENGTH}sH")

# Signed alongside the payload, so a signature made for this purpose cannot be replayed into
# another one, and a valid object cannot be re-filed under a different action.
DOMAIN = b"tine-cache-ac-v1\0"

# Hex characters in a SHA-256, which is the only digest function this cache advertises.
HASH_LENGTH = 64


def key_id(public: PublicKeyTypes) -> bytes:
    """A short name for a key: the start of the SHA-256 of its DER SubjectPublicKeyInfo."""
    return hashlib.sha256(
        public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ).digest()[:KEY_ID_LENGTH]


def public_pem(public: PublicKeyTypes) -> str:
    """The key as a PEM block, which is how a bare one is written down."""
    return public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def is_sha256_hex(text: str) -> bool:
    """Whether this is the lower-case hex of a SHA-256, which is how every digest here is named."""
    return len(text) == HASH_LENGTH and not text.strip("0123456789abcdef")


def _signed(action_hash: str, payload: bytes) -> bytes:
    """The bytes a signature covers: the purpose, the action, the payload.

    Nothing separates the three, so the hash has to be a fixed width or a shorter one could take
    its first bytes from the payload. Buck2 only ever sends 64 hex characters; this makes that a
    property of the format rather than of the client.
    """
    if not is_sha256_hex(action_hash):
        raise ValueError(f"not a SHA-256 action hash: {action_hash!r}")
    return DOMAIN + action_hash.encode("ascii") + payload


class Signer:
    """The builder's half. Only a machine that produces results has one.

    The certificate is optional because a flat list of trusted keys needs none. Where there is one,
    it is checked here to be for this key: publishing a certificate for a *different* key would make
    every result this builder writes unreadable, with nothing to say why.

    `object_lifetime` is how long the bucket keeps what this signs. A reader wants the certificate
    valid when it *reads*, and a pointer is read for as long as it is in the bucket, so a signature
    made with less than that left on the certificate is a result that will be refused before it is
    expired. Refusing to make it is the builder's half of that rule; the other half is whoever
    provisions the leaf giving it a validity of at least a rotation period plus this.
    """

    def __init__(
        self,
        key_file: Path,
        certificate: Path | None = None,
        object_lifetime: datetime.timedelta = datetime.timedelta(),
    ) -> None:
        self.object_lifetime = object_lifetime
        raw = key_file.read_bytes()
        # OpenSSH's own format is PEM-armoured too, and `ssh-keygen -t ed25519` is how a key often
        # arrives, so take either rather than making the caller convert.
        loader = (
            serialization.load_ssh_private_key
            if raw.lstrip().startswith(b"-----BEGIN OPENSSH")
            else serialization.load_pem_private_key
        )
        key = loader(raw, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{key_file} is a {type(key).__name__}; a signing key is Ed25519")
        self.key = key
        self.public = key.public_key()
        self.id = key_id(self.public)
        self.certificate_pem = certificate.read_text(encoding="utf-8") if certificate else None
        self.certificate = load_certificate(self.certificate_pem) if self.certificate_pem else None
        if self.certificate is not None and key_id(self.certificate.public_key()) != self.id:
            raise ValueError(f"{certificate} is not a certificate for signing key {self.id.hex()}")

    def check_current(self) -> None:
        """Raise `ValueError` unless a result signed now would still be readable when it expires."""
        if self.certificate is None:
            return
        now = datetime.datetime.now(datetime.UTC)
        check_window(self.certificate, "signing certificate", now)
        until = self.certificate.not_valid_after_utc
        if now + self.object_lifetime > until:
            raise ValueError(
                f"signing certificate expires on {until:%Y-%m-%d}, before a result signed now would"
                f" leave the bucket; time to rotate"
            )

    def wrap(self, action_hash: str, payload: bytes) -> bytes:
        self.check_current()
        signature = self.key.sign(_signed(action_hash, payload))
        return HEADER.pack(MAGIC, VERSION, ED25519, self.id, len(signature)) + signature + payload


def verify(public: PublicKeyTypes, algorithm: int, signature: bytes, message: bytes) -> None:
    """Raise `InvalidSignature` unless this key made this signature over these bytes."""
    if algorithm != ED25519 or not isinstance(public, Ed25519PublicKey):
        # The envelope names an algorithm and the key is another: refuse rather than let the key
        # decide, so the named algorithm is never a thing an attacker can steer.
        raise InvalidSignature("algorithm does not match the key")
    public.verify(signature, message)


def split(blob: bytes) -> tuple[bytes, int, bytes, bytes]:
    """Take a stored object apart: key id, algorithm, signature, payload.

    Every length is checked against what is actually there, so a truncated or overlong object is a
    refusal rather than a short read of whatever followed it.
    """
    if len(blob) < HEADER.size:
        raise ValueError(f"stored object is {len(blob)} bytes, too short to be signed")
    # struct hands back untyped values; these are what the format string says they are.
    magic, version, algorithm, named, length = cast(
        tuple[bytes, int, int, bytes, int], HEADER.unpack_from(blob)
    )
    if magic != MAGIC:
        raise ValueError("stored object is not signed")
    if version != VERSION:
        raise ValueError(f"signature version {version} is not one this knows")
    if algorithm != ED25519:
        raise ValueError(f"signature algorithm {algorithm} is not one this knows")
    signature = blob[HEADER.size : HEADER.size + length]
    if len(signature) != length:
        raise ValueError(f"signature says {length} bytes, {len(signature)} are there")
    return named, algorithm, signature, blob[HEADER.size + length :]


type Basic = x509.BasicConstraints | None
type Usage = x509.KeyUsage | None


def _leaf_not_a_ca(policy: Policy, certificate: x509.Certificate, basic: Basic) -> None:
    if basic is not None and basic.ca:
        raise VerificationError("the leaf is a CA, not a signing key")


def _leaf_may_sign(policy: Policy, certificate: x509.Certificate, usage: Usage) -> None:
    if usage is not None and not usage.digital_signature:
        raise VerificationError("the leaf is not for signing")


def _authority_is_a_ca(policy: Policy, certificate: x509.Certificate, basic: Basic) -> None:
    if basic is not None and not basic.ca:
        raise VerificationError("the authority is not marked as a CA")


def _authority_may_issue(policy: Policy, certificate: x509.Certificate, usage: Usage) -> None:
    if usage is not None and not usage.key_cert_sign:
        raise VerificationError("the authority may not sign certificates")


# What a leaf has to say about itself, in so many words. X.509 reads an absent KeyUsage as
# unrestricted; here a leaf without one is refused, since that is what an extensions-less request
# produces and the design asks for a leaf that says what it is for. Anything else a certificate
# carries is allowed, because a leaf for signing cache results has no reason to carry the names and
# usages a TLS certificate needs.
LEAF_POLICY = (
    ExtensionPolicy.permit_all()
    .require_present(x509.BasicConstraints, Criticality.AGNOSTIC, _leaf_not_a_ca)
    .require_present(x509.KeyUsage, Criticality.AGNOSTIC, _leaf_may_sign)
)
AUTHORITY_POLICY = (
    ExtensionPolicy.permit_all()
    .require_present(x509.BasicConstraints, Criticality.AGNOSTIC, _authority_is_a_ca)
    .may_be_present(x509.KeyUsage, Criticality.AGNOSTIC, _authority_may_issue)
)


class Certificates(Protocol):
    """Where leaf certificates that passed the chain check are kept between lookups.

    The shim's store keeps them on disk, beside the pointers they signed for: a pointer kept locally
    is verified again on every hit, and after a restart that has to work with the bucket unreachable.
    """

    def certificate(self, named: bytes) -> str | None: ...

    def put_certificate(self, named: bytes, pem: str) -> None: ...


class Remembered:
    """Certificates kept in memory, for an authority given nowhere better."""

    def __init__(self) -> None:
        self._known: dict[bytes, str] = {}
        self._lock = threading.Lock()

    def certificate(self, named: bytes) -> str | None:
        with self._lock:
            return self._known.get(named)

    def put_certificate(self, named: bytes, pem: str) -> None:
        with self._lock:
            self._known[named] = pem


class Authority:
    """What decides which results a shim will serve: certificates under a CA.

    A key that signs every result is used constantly and cannot live anywhere expensive, so it is an
    ordinary file on the build server. A key that only ever signs *that* key is used once a rotation
    and can live in a TPM, which is where infrastructure wants the thing everyone has to trust.

    So readers are given the certificate authority and nothing else, and a leaf certificate travels
    in the bucket beside the objects it signed for. Rotation is then a new leaf and no change
    anywhere a reader can see, which is the whole reason for the indirection.

    The leaf must be valid *now*, by this machine's clock. A signing time in the payload would be
    worthless instead: whoever holds the key chooses that number, so a stolen key would simply claim
    a time inside the window, which is why a pointer carries none. What makes "valid now" affordable
    is that the bucket expires objects too, so a leaf outliving the bucket's object lifetime orphans
    nothing.
    """

    def __init__(
        self,
        authorities: Iterable[str],
        fetch: Callable[[str], bytes | None],
        remembered: Certificates | None = None,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self.clock = clock or (lambda: datetime.datetime.now(datetime.UTC))
        self.authorities: list[x509.Certificate] = []
        for one in authorities:
            authority = load_certificate(one)
            basic = _extension(authority, x509.BasicConstraints)
            if basic is None or not basic.ca:
                raise ValueError("authority certificate is not marked as a CA")
            try:
                check_window(authority, "authority certificate", self.clock())
            except ValueError as error:
                # Rotating the CA means readers carry the old one beside the new for a while, and
                # the day the old one expires must not be the day every reader refuses to start.
                # Loud, because it is a configuration that is now overdue for cleaning up.
                log.warning("ignoring %s", error)
                continue
            self.authorities.append(authority)
        if not self.authorities:
            raise ValueError("an authority with no current certificates would trust nothing")
        self.store = Store(self.authorities)
        self.fetch = fetch
        self.remembered = remembered or Remembered()

    def describe(self) -> str:
        named = ", ".join(key_id(one.public_key()).hex() for one in self.authorities)
        return f"certificates under {named}"

    def unwrap(self, action_hash: str, blob: bytes) -> bytes:
        """The payload, if a key we trust signed exactly these bytes under this action."""
        named, algorithm, signature, payload = split(blob)
        key = self.key_for(named)
        if key is None:
            raise ValueError(f"signed by {named.hex()}, which is not trusted here")
        try:
            verify(key, algorithm, signature, _signed(action_hash, payload))
        except InvalidSignature:
            raise ValueError(f"signature by {named.hex()} does not match these bytes") from None
        return payload

    def admit(self, signer: Signer) -> None:
        """Raise `ValueError` saying why, unless what this signer writes is something we would read back.

        A builder that signs with a key nobody trusts fills a cache it cannot use itself, which is
        worth refusing at startup rather than discovering a build later. Checked against the
        certificate in hand, before it is published anywhere: fetching it back the way a reader
        would is no test of anything until it has been published, and publishing one that fails
        would leave a bad certificate in the bucket for every reader to refuse. Remembered once
        accepted, so the builder's own reads never go and fetch it.
        """
        if signer.certificate is None or signer.certificate_pem is None:
            raise ValueError(f"signing key {signer.id.hex()} has no certificate to publish")
        self.check(signer.certificate, signer.id)
        self.remembered.put_certificate(signer.id, signer.certificate_pem)

    def key_for(self, named: bytes) -> PublicKeyTypes | None:
        """The key that id names, if it is one we would act on right now.

        What is remembered is the certificate, not a verdict about it: whether it is valid changes
        every second, so that is asked every time. A shim lives for as long as builds keep arriving,
        and a leaf accepted this morning is exactly the key an attacker would like it to still
        accept tonight.

        A remembered certificate that is refused is fetched once more before anything is refused.
        Renewing a certificate keeps the key, and so keeps the name it is filed under, so the one in
        the bucket may well be newer than the one in hand. A leaf that really has expired costs one
        request per lookup until its results leave the bucket, which is the price of not caching a
        refusal.
        """
        remembered = self.remembered.certificate(named)
        if remembered is not None:
            try:
                return self.check(load_certificate(remembered), named)
            except ValueError as error:
                log.info("%s; asking for a newer one", error)
        material = self.fetch(certificate_key(named))
        if material is None:
            return None
        pem = material.decode(errors="replace")
        key = self.check(load_certificate(pem), named)
        self.remembered.put_certificate(named, pem)
        return key

    def check(self, leaf: x509.Certificate, named: bytes) -> PublicKeyTypes:
        """The leaf's key, if the whole chain is one we would act on right now.

        The chain, both validity windows and the extensions are the library's business, built
        fresh with this machine's clock on every call: that costs less than one signature check,
        and it is the check that bounds a stolen key. What is ours is that the key is the one kind
        this signs with, and that it is the key the certificate was filed under: a store that
        answers for the wrong key is broken, and quietly working around that would hide it.
        """
        verifier = (
            PolicyBuilder()
            .store(self.store)
            .time(self.clock())
            .max_chain_depth(0)
            .extension_policies(ca_policy=AUTHORITY_POLICY, ee_policy=LEAF_POLICY)
            .build_client_verifier()
        )
        try:
            verifier.verify(leaf, [])
        except VerificationError as error:
            raise ValueError(f"certificate for {named.hex()} refused: {error}") from None
        key = leaf.public_key()
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError(f"certificate for {named.hex()} holds a {type(key).__name__}, not Ed25519")
        found = key_id(key)
        if found != named:
            raise ValueError(f"certificate filed under {named.hex()} holds key {found.hex()}")
        return key


def certificate_key(named: bytes) -> str:
    """Where a leaf certificate lives, which is the one thing a reader has to go and get."""
    return f"keys/{named.hex()}"


def load_certificate(pem: str) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(pem.strip().encode())
    except (ValueError, UnsupportedAlgorithm, x509.InvalidVersion) as error:
        # InvalidVersion is cryptography's own class for a version field outside 0..2, a one-byte
        # edit of a real certificate; the object came from the bucket, so it is refused like the rest.
        raise ValueError(f"not a usable certificate: {error}") from None


def check_window(certificate: x509.Certificate, what: str, now: datetime.datetime) -> None:
    """Reject a certificate outside its own validity, by this machine's clock and nothing else."""
    if now < certificate.not_valid_before_utc:
        raise ValueError(f"{what} is not valid until {certificate.not_valid_before_utc:%Y-%m-%d}")
    if now > certificate.not_valid_after_utc:
        raise ValueError(f"{what} expired on {certificate.not_valid_after_utc:%Y-%m-%d}")


def _extension[T: x509.ExtensionType](certificate: x509.Certificate, kind: type[T]) -> T | None:
    try:
        return certificate.extensions.get_extension_for_class(kind).value
    except x509.ExtensionNotFound:
        return None
