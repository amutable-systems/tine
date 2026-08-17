# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""deb repositories, pinned to snapshot.debian.org."""

load("//package:repository.bzl", "RepositoryPin", "declare_remote_repository")

PACKAGE_SYSTEM = "@tine//package_system/deb:package_system"

# snapshot.debian.org serves one immutable tree per timestamp. The last path component selects
# the archive: `debian-security` is another archive next to `debian`.
ARCHIVE_MIRROR = "https://snapshot.debian.org/archive/debian"

def _check_snapshot(snapshot: str) -> None:
    if len(snapshot) != 16 or snapshot[8] != "T" or snapshot[-1] != "Z" or not (snapshot[:8] + snapshot[9:15]).isdigit():
        fail("archive_snapshot must be a YYYYMMDDTHHMMSSZ archive timestamp: {}".format(snapshot))

def _pinned_at(snapshot: str) -> str:
    """Return the archive timestamp as ISO 8601. The tree holds what the archive had published by then."""
    return "{}-{}-{}T{}:{}:{}Z".format(snapshot[0:4], snapshot[4:6], snapshot[6:8], snapshot[9:11], snapshot[11:13], snapshot[13:15])

def deb_remote_repository(
    name: str,
    suite: str,
    architectures: list[str],
    component: str | None = None,
    baseurl: str | None = None,
    archive_mirror: str | None = None,
    archive_snapshot: str | None = None,
    **kwargs,
) -> None:
    """Declare a Debian repository, optionally pinned to a snapshot.debian.org timestamp.

    A live mirror replaces its index within hours, and a committed snapshot then no longer builds.
    The timestamped trees of snapshot.debian.org never change. One tine repository is one component
    of a suite for all architectures. Debian puts the architecture into the path of the index and
    not into the mirror URL, so all architectures share the base URL.
    """

    # The component defaults to the last part of the name: `main` for `debian.trixie.main.repository`.
    component = component or name.removesuffix(".repository").split(".")[-1]
    if (archive_mirror == None) != (archive_snapshot == None):
        fail("deb_remote_repository requires archive_mirror and archive_snapshot together: {}".format(name))
    pin = None
    if archive_mirror != None:
        _check_snapshot(archive_snapshot)
        pin = RepositoryPin(
            baseurl = "{}/{}".format(archive_mirror.rstrip("/"), archive_snapshot),
            metadata = {
                "debian.mirror": archive_mirror,
                "debian.snapshot": archive_snapshot,
            },
            pinned_at = _pinned_at(archive_snapshot),
        )
    declare_remote_repository(
        name = name,
        what = "deb_remote_repository",
        label = "tine:deb-remote-repository",
        package_system = PACKAGE_SYSTEM,
        architectures = architectures,
        baseurl = baseurl,
        pin = pin,
        # The base URL has no architecture, so these fields are the same for every architecture.
        # The snapshot spec that all package systems share adds `arch`.
        snapshot_spec = {"component": component, "suite": suite},
        **kwargs,
    )
