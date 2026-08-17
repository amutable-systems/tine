# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""deb repositories, pinned to snapshot.debian.org."""

load("//package:repository.bzl", "RepositoryPin", "declare_remote_repository")
load("//platforms:architecture.bzl", "architecture")

PACKAGE_SYSTEM = "@tine//package_system/deb:package_system"

# The archive's timestamped trees, whose indexes never change under a committed pin.
ARCHIVE_MIRROR = "https://snapshot.debian.org/archive"

def _check_snapshot(snapshot: str) -> None:
    if len(snapshot) != 16 or snapshot[8] != "T" or snapshot[-1] != "Z" or not (snapshot[:8] + snapshot[9:15]).isdigit():
        fail("archive_snapshot must be a YYYYMMDDTHHMMSSZ archive timestamp: {}".format(snapshot))

def _pinned_at(snapshot: str) -> str:
    """The archive timestamp as ISO 8601: the tree holds what was published up to then."""
    return "{}-{}-{}T{}:{}:{}Z".format(snapshot[0:4], snapshot[4:6], snapshot[6:8], snapshot[9:11], snapshot[11:13], snapshot[13:15])

def deb_remote_repository(
    name: str,
    suite: str,
    architectures: list[str],
    component: str | None = None,
    baseurl: str | None = None,
    archive_mirror: str | None = None,
    archive_snapshot: str | None = None,
    archive: str = "debian",
    **kwargs,
) -> None:
    """Declare a Debian repository, optionally pinned to a snapshot.debian.org timestamp.

    A live mirror serves an index that changes under the committed pin within hours, so only the
    archive's timestamped trees keep a snapshot buildable. One tine repository is one suite
    component for one architecture, because that is the one Packages index a solve reads; Debian
    names the architecture in the index path rather than in the mirror URL, so the declaration
    takes the single architecture it serves and spells it for the archive.
    """
    if len(architectures) != 1:
        fail("deb_remote_repository serves one architecture, not {}: {}".format(architectures, name))
    arch = architecture.spelling(architectures[0], "deb")

    # `debian.trixie.main.repository` indexes the `main` component, unless the caller says otherwise.
    component = component or name.removesuffix(".repository").split(".")[-1]
    if (archive_mirror == None) != (archive_snapshot == None):
        fail("deb_remote_repository requires archive_mirror and archive_snapshot together: {}".format(name))
    pin = None
    if archive_mirror != None:
        _check_snapshot(archive_snapshot)
        pin = RepositoryPin(
            baseurl = "{}/{}/{}".format(archive_mirror.rstrip("/"), archive, archive_snapshot),
            metadata = {
                "debian.arch": arch,
                "debian.archive": archive,
                "debian.component": component,
                "debian.mirror": archive_mirror,
                "debian.snapshot": archive_snapshot,
                "debian.suite": suite,
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
        snapshot_spec = {"arch": arch, "component": component, "suite": suite},
        # What the Release has to state the pinned index under, which only this declaration knows.
        verify_spec = {"arch": arch, "component": component},
        **kwargs,
    )
