"""Opinionated RPM-family catalog declarations."""

load("//distribution:defs.bzl", "distribution")
load("//package:buildroot.bzl", "buildroot")
load("//package:manager.bzl", "package_manager")
load("//package:release.bzl", "os_release")
load("//package:repository.bzl", "repository_universe")
load(":rules.bzl", "rpm_remote_repository")

_RPM = "@tine//package_system/rpm:package_system"

_FEDORA_PACKAGE_SETS = {
    "bootable": [
        "bash",
        "coreutils",
        "dbus-broker",
        "fedora-release",
        "kernel-core",
        "systemd",
        "systemd-boot-unsigned",
        "systemd-udev",
        "util-linux",
    ],
    "buildroot": ["@buildsys-build"],
    "initrd": ["bash", "kmod", "systemd", "systemd-udev", "veritysetup"],
}

def _merge_package_sets(
    defaults: dict[str, list[str]],
    overrides: dict[str, list[str]],
) -> dict[str, list[str]]:
    package_sets = dict(defaults)
    package_sets.update(overrides)
    return package_sets

def _check_name(name: str, family: str, version: str) -> None:
    expected = "{}.{}".format(family, version)
    if name != expected:
        fail("{} release name must be {!r}".format(family, expected))

def fedora_release(
    name: str,
    version: str,
    engine: str,
    baseurl: str | None = None,
    rpmrepo_mirror: str | None = None,
    rpmrepo_snapshot: str | None = None,
    package_set_overrides: dict[str, list[str]] = {},
    additional_repositories: list[str] = [],
    repository_priorities: dict[str, int] = {},
    visibility: list[str] | None = None,
) -> None:
    """Declare the conventional Fedora release target bundle."""
    _check_name(name, "fedora", version)
    if rpmrepo_mirror == None and baseurl == None:
        if version == "rawhide":
            baseurl = "https://dl.fedoraproject.org/pub/fedora/linux/development/rawhide/Everything/x86_64/os/"
        else:
            baseurl = "https://dl.fedoraproject.org/pub/fedora/linux/releases/{}/Everything/x86_64/os/".format(version)

    rpm_remote_repository(
        name = name + ".repository",
        baseurl = baseurl,
        rpmrepo_mirror = rpmrepo_mirror,
        rpmrepo_snapshot = rpmrepo_snapshot,
    )
    repository_universe(
        name = name + ".repositories",
        package_system = _RPM,
        required_repositories = [":" + name + ".repository"],
    )
    distribution(name = name + ".distribution", visibility = visibility)
    os_release(
        name = name + ".release",
        repository_universe = ":" + name + ".repositories",
        package_sets = _merge_package_sets(_FEDORA_PACKAGE_SETS, package_set_overrides),
        visibility = visibility,
    )
    package_manager(
        name = name + ".package-manager",
        release = ":" + name + ".release",
        engine = engine,
        additional_repositories = additional_repositories,
        repository_priorities = repository_priorities,
        visibility = visibility,
    )
    buildroot(
        name = name + ".buildroot",
        package_manager = ":" + name + ".package-manager",
        package_set = "buildroot",
        visibility = visibility,
    )
