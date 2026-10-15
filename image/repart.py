# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Signing keys, mkfs options and report parsing for the drivers that run systemd-repart."""

from pathlib import Path
from typing import Any, TypedDict

from util import fail


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


def mkfs_environment(options: dict[str, list[str]]) -> dict[str, str]:
    """Return the systemd-repart environment that passes `options` to mkfs, per filesystem."""
    erofs = options.get("erofs", [])
    # The kernel shares the page cache of identical files between erofs images mounted with
    # inode_share, but only for images that record a digest of each file.
    if not any(option.startswith("--xattr-inode-digest") for option in erofs):
        erofs = [*erofs, "--xattr-inode-digest"]
    # repart reads one variable per filesystem it formats and splits each on whitespace.
    return {
        f"SYSTEMD_REPART_MKFS_OPTIONS_{filesystem.upper()}": " ".join(arguments)
        for filesystem, arguments in (options | {"erofs": erofs}).items()
    }


def write_root_hash(rows: list[dict[str, Any]], output: Path) -> None:
    """Write the verity root hash repart generated, as hex plus a newline.

    The rows are repart's --json output.
    """
    # TBD: placeholder of a partition repart did not generate
    hashes = {value for row in rows if (value := row.get("roothash")) not in (None, "TBD")}
    if len(hashes) != 1:
        fail(f"repart: expected one generated verity root hash, found {len(hashes)}")
    output.write_text(hashes.pop() + "\n")
