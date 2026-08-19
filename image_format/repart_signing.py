"""Key material for systemd-repart's verity signing, shared by the drivers that invoke it."""

from typing import TypedDict


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
