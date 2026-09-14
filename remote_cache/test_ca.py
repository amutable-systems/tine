"""Keys and certificates for tests.

The two kinds of key anything here signs with are a TPM-shaped authority (ECDSA P-256 because a TPM has
no Ed25519), and a file-shaped leaf (Ed25519 because it is only a file and can be the fast algorithm).

As a program, it makes a throwaway CA and a leaf certificate under it, for a test bucket:

    buck run tine//remote_cache:test-ca -- <directory>

Writes `ca.pem`, `leaf.key` and `leaf.pem` into the directory: what a `[cache]` table takes as
`authority`, `signing_key` and `signing_certificate`. The shape is the shipped one (docs/remote-cache.md,
"The keys"), so a test meets exactly the checks a deployment does.
"""

import argparse
import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

import signing

DAY = datetime.timedelta(days=1)
AUTHORITY = "test CA"

type Signing = Ed25519PrivateKey | ec.EllipticCurvePrivateKey


def name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def issue(
    subject: str,
    key: Signing,
    issuer_name: x509.Name,
    issuer_key: Signing,
    *,
    ca: bool = False,
    valid_from: datetime.timedelta = -DAY,
    valid_to: datetime.timedelta = 365 * DAY,
    digital_signature: bool = True,
    extensions: bool = True,
) -> str:
    """One certificate, as PEM. Without `extensions`, the bare kind an extensions-less request makes."""
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name(subject))
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + valid_from)
        .not_valid_after(now + valid_to)
    )
    algorithm = None if isinstance(issuer_key, Ed25519PrivateKey) else hashes.SHA256()
    if not extensions:
        return builder.sign(issuer_key, algorithm).public_bytes(serialization.Encoding.PEM).decode()
    builder = builder.add_extension(
        x509.BasicConstraints(ca=ca, path_length=None), critical=True
    ).add_extension(
        x509.KeyUsage(
            digital_signature=digital_signature,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=ca,
            crl_sign=ca,
            encipher_only=False,
            decipher_only=False,
        ),
        critical=True,
    )
    return builder.sign(issuer_key, algorithm).public_bytes(serialization.Encoding.PEM).decode()


def authority(common: str = AUTHORITY) -> tuple[ec.EllipticCurvePrivateKey, str]:
    """A self-signed CA: its key, and its certificate as PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key, issue(common, key, name(common), key, ca=True)


def write_key(path: Path, key: Signing, openssh: bool = False) -> Path:
    """The private key on disk, as PKCS8 PEM the way `openssl genpkey` leaves it, or as `ssh-keygen` does."""
    form = serialization.PrivateFormat.OpenSSH if openssh else serialization.PrivateFormat.PKCS8
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, form, serialization.NoEncryption()))
    return path


def ed25519(directory: Path, named: str, openssh: bool = False) -> tuple[Path, str]:
    """A fresh leaf-shaped key on disk, and its public half as PEM."""
    key = Ed25519PrivateKey.generate()
    return write_key(directory / named, key, openssh), signing.public_pem(key.public_key())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    directory = parser.parse_args().directory
    directory.mkdir(parents=True, exist_ok=True)
    ca_key, ca_pem = authority()
    (directory / "ca.pem").write_text(ca_pem)
    leaf = Ed25519PrivateKey.generate()
    write_key(directory / "leaf.key", leaf)
    (directory / "leaf.pem").write_text(issue("builder", leaf, name(AUTHORITY), ca_key))


if __name__ == "__main__":
    main()
