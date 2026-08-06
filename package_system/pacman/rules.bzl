"""alpm repositories, pinned to the Arch Linux Archive."""

load("//package:repository.bzl", "RepositoryPin", "declare_remote_repository")

_PACMAN_PACKAGE_SYSTEM = "@tine//package_system/pacman:package_system"

# The Arch Linux Archive's dated trees, whose databases never change under a committed pin.
ARCHIVE_MIRROR = "https://archive.archlinux.org/repos"

def pacman_remote_repository(
    name: str,
    repository: str | None = None,
    baseurl: str | None = None,
    archive_mirror: str | None = None,
    archive_snapshot: str | None = None,
    arch: str = "x86_64",
    **kwargs,
) -> None:
    """Declare an alpm repository, optionally pinned to an Arch Linux Archive day.

    A rolling mirror serves a database that changes under the committed pin, so only the archive's
    dated trees keep a snapshot buildable.
    """

    # `arch.core.repository` serves `core.db`, unless the caller says otherwise.
    repository = repository or name.removesuffix(".repository").split(".")[-1]
    if (archive_mirror == None) != (archive_snapshot == None):
        fail("pacman_remote_repository requires archive_mirror and archive_snapshot together: {}".format(name))
    pin = None
    if archive_mirror != None:
        day = archive_snapshot.split("/")
        if len(day) != 3 or [len(part) for part in day] != [4, 2, 2] or not "".join(day).isdigit():
            fail("archive_snapshot must be a YYYY/MM/DD archive day: {}".format(archive_snapshot))
        pin = RepositoryPin(
            baseurl = "{}/{}/{}/os/{}".format(archive_mirror.rstrip("/"), archive_snapshot, repository, arch),
            metadata = {
                "archlinux.arch": arch,
                "archlinux.mirror": archive_mirror,
                "archlinux.repository": repository,
                "archlinux.snapshot": archive_snapshot,
            },
        )
    declare_remote_repository(
        name = name,
        what = "pacman_remote_repository",
        label = "tine:pacman-remote-repository",
        package_system = _PACMAN_PACKAGE_SYSTEM,
        baseurl = baseurl,
        pin = pin,
        snapshot_spec = {"db": repository + ".db"},
        **kwargs,
    )
