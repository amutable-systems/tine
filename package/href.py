# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Validate the locations repository metadata claims for its packages and streams.

A repository names its own content, so a snapshot has to reject anything that would leave the
repository, change meaning after URL handling, or become a path a driver did not intend to
write. Every package system's snapshot driver runs its locations through this.
"""

from urllib.parse import unquote, urlsplit

from util import fail


def relative_href(rid: str, what: str, href: str | None) -> str:
    """Check that `href` is a plain repository-relative path, and return it unchanged."""
    if not href:
        fail(f"{rid}: {what} has an empty location")
    parsed = urlsplit(href)

    # An alpm package carries its epoch as `fakeroot-1:1.37.2-2-...`, which parses as a scheme.
    # An epoch is digits appended to a name with a dash, and what follows a scheme's colon here
    # would have to be a path. Carving out exactly that shape keeps the rest of the scheme check,
    # rather than dropping it because one package system's names contain a colon at all.
    epoch = bool(parsed.scheme) and parsed.scheme.rstrip("0123456789").endswith("-")
    if epoch and href[len(parsed.scheme) + 1 :].startswith("/"):
        epoch = False

    # Decode to a fixed point so nested escapes cannot conceal traversal or separators.
    decoded = href
    while True:
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded

    parts = decoded.split("/")
    external = bool((parsed.scheme and not epoch) or parsed.netloc or parsed.query or parsed.fragment)
    invalid_path = (
        decoded.startswith("/")
        or decoded.endswith("/")
        # Reject encoded slashes that would change the path after URL handling.
        or decoded.count("/") != href.count("/")
        or any(part in ("", ".", "..") for part in parts)
        or "\\" in decoded
    )
    non_ascii = any(ord(character) > 127 for character in href)
    control_character = any(ord(character) < 32 or ord(character) == 127 for character in decoded)
    if external or invalid_path or non_ascii or control_character:
        fail(f"{rid}: {what} has unsupported location {href!r}")
    return href
