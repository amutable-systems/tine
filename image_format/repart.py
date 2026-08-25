"""What the drivers invoking systemd-repart share: signing key material, and reading its report back."""

from pathlib import Path
from typing import Any, TypedDict


class KeySpec(TypedDict):
    """One signing key as a driver-spec object; sign.bzl's signing_key_spec() produces it."""

    private_key: str
    certificate: str
    # OpenSSL sources in systemd's spelling, each None for material in the build graph.
    private_key_source: str | None
    certificate_source: str | None


def key_arguments(signing: KeySpec | None) -> list[str]:
    """systemd-repart options selecting the key that signs a verity root hash."""
    if signing is None:
        return []
    arguments = ["--private-key", signing["private_key"], "--certificate", signing["certificate"]]
    if signing["private_key_source"]:
        arguments += ["--private-key-source", signing["private_key_source"]]
    if signing["certificate_source"]:
        arguments += ["--certificate-source", signing["certificate_source"]]
    return arguments


def write_root_hash(rows: list[dict[str, Any]], output: Path) -> None:
    """Write the verity root hash repart generated, as hex plus a newline.

    The rows are repart's --json output.
    """
    # TBD: placeholder of a partition repart did not generate
    hashes = {value for row in rows if (value := row.get("roothash")) not in (None, "TBD")}
    if len(hashes) != 1:
        raise SystemExit(f"repart: expected one generated verity root hash, found {len(hashes)}")
    output.write_text(hashes.pop() + "\n")
