# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Parse the names that go.dev gives Go releases, such as go1.26.10."""

import re

_PATCH_RELEASE = re.compile(r"go([0-9]+)\.([0-9]+)\.([0-9]+)")


def patch_release(release: object) -> tuple[int, int, int] | None:
    """Return the major, minor and patch version of a release such as go1.26.10, or None for another name."""
    match = _PATCH_RELEASE.fullmatch(release) if isinstance(release, str) else None
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))
