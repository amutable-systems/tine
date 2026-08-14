"""The conventional Arch Linux distribution declaration."""

load("//distribution:defs.bzl", "distribution")
load("//package:manager.bzl", "package_manager")
load("//package:release.bzl", "os_release")
load("//package:repository.bzl", "repository_universe")
load("//package_system/pacman:rules.bzl", "ARCHIVE_MIRROR", "PACKAGE_SYSTEM", "pacman_remote_repository")

_ARCH_PACKAGE_SETS = {
    "bootable": [
        "bash",
        "coreutils",
        "dbus-broker",
        "dbus-broker-units",
        "linux",
        "systemd",
        "systemd-sysvcompat",
        "util-linux",
    ],
    # `base-devel` is a metapackage, so makepkg's assumed build environment is one name.
    "buildroot": ["base-devel"],
    # Arch ships udev inside systemd and veritysetup inside cryptsetup.
    "initrd": ["bash", "cryptsetup", "kmod", "systemd"],
}

# Arch has no numbered releases; a catalog pins a date in the archive instead.
_COMPONENTS = ("core", "extra", "multilib")

def _check_name(name: str) -> None:
    if not name.startswith("arch.") or name.count(".") != 1:
        fail("arch release name must be 'arch.<release>': {}".format(name))

def arch_release(
    name: str,
    box: str,
    archive_snapshot: str | None = None,
    archive_mirror: str = ARCHIVE_MIRROR,
    arch: str = "x86_64",
    repository_urls: dict[str, str] = {},
    package_set_overrides: dict[str, list[str]] = {},
    enable_repository_groups: list[str] = [],
    disable_repository_groups: list[str] = [],
    additional_repositories: list[str] = [],
    repository_priorities: dict[str, int] = {},
    visibility: list[str] | None = None,
) -> None:
    """Declare the conventional Arch Linux release target bundle.

    Every repository shares one archive pin, so the release stays internally consistent: core and
    extra are only guaranteed to solve together when they come from the same day. Overriding the
    mirrors is therefore all or nothing, since a release with one repository off the pin would
    have refresh-catalog advance its siblings past it.
    """
    _check_name(name)
    unknown = [component for component in repository_urls if component not in _COMPONENTS]
    if unknown:
        fail("arch_release: unknown repository components {}".format(sorted(unknown)))
    if (archive_snapshot == None) == (not repository_urls):
        fail("arch_release: takes archive_snapshot or repository_urls, not both or neither: {}".format(name))
    if repository_urls and sorted(repository_urls) != sorted(_COMPONENTS):
        fail(
            "arch_release: repository_urls must cover every component {}: {}".format(
                sorted(_COMPONENTS),
                name,
            ),
        )

    for component in _COMPONENTS:
        pacman_remote_repository(
            name = "{}.{}.repository".format(name, component),
            repository = component,
            arch = arch,
            baseurl = repository_urls.get(component),
            archive_mirror = archive_mirror if archive_snapshot else None,
            archive_snapshot = archive_snapshot,
        )

    repository_universe(
        name = name + ".repositories",
        package_system = PACKAGE_SYSTEM,
        required_repositories = [":" + name + ".core.repository"],
        optional_repository_groups = {
            "extra": [":" + name + ".extra.repository"],
            "multilib": [":" + name + ".multilib.repository"],
        },
        default_repository_groups = ["extra"],
    )
    distribution(name = name + ".distribution", visibility = visibility)
    package_sets = dict(_ARCH_PACKAGE_SETS)
    package_sets.update(package_set_overrides)
    os_release(
        name = name + ".release",
        repository_universe = ":" + name + ".repositories",
        package_sets = package_sets,
        visibility = visibility,
    )
    package_manager(
        name = name + ".package-manager",
        release = ":" + name + ".release",
        box = box,
        enable_repository_groups = enable_repository_groups,
        disable_repository_groups = disable_repository_groups,
        additional_repositories = additional_repositories,
        repository_priorities = repository_priorities,
        visibility = visibility,
    )
