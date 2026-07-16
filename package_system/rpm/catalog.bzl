"""Opinionated RPM-family catalog declarations."""

load("//package:buildroot.bzl", "buildroot")
load("//package:manager.bzl", "package_manager")
load("//package:release.bzl", "os_release")
load("//package:repository.bzl", "repository_universe")
load(":rules.bzl", "rpm_remote_repository")

_RPM = "@tine//package_system/rpm:package_system"

_FEDORA_PACKAGE_SETS = {
    "buildroot": ["@buildsys-build"],
    "initrd": ["bash", "kmod", "systemd", "systemd-udev", "veritysetup"],
}

_CENTOS_STREAM_PACKAGE_SETS = {
    "buildroot": [
        "bash",
        "bzip2",
        "centos-stream-release",
        "coreutils",
        "cpio",
        "diffutils",
        "findutils",
        "gawk",
        "glibc-minimal-langpack",
        "grep",
        "gzip",
        "info",
        "patch",
        "redhat-rpm-config",
        "rpm-build",
        "sed",
        "shadow-utils",
        "tar",
        "unzip",
        "util-linux",
        "which",
        "xz",
    ],
    "initrd": ["bash", "kmod", "systemd", "systemd-udev", "veritysetup"],
}

def _merge_package_sets(
        defaults: dict[str, list[str]],
        overrides: dict[str, list[str]]) -> dict[str, list[str]]:
    package_sets = dict(defaults)
    package_sets.update(overrides)
    return package_sets

def _check_name(name: str, family: str, version: str) -> None:
    expected = "{}.{}".format(family, version)
    if name != expected:
        fail("{} release name must be {!r}".format(family, expected))

# buildifier: disable=function-docstring-args
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
        visibility: list[str] | None = None) -> None:
    """Declare the conventional Fedora release target bundle."""
    _check_name(name, "fedora", version)
    if (rpmrepo_mirror == None) != (rpmrepo_snapshot == None):
        fail("fedora_release requires rpmrepo_mirror and rpmrepo_snapshot together: {}".format(name))
    if rpmrepo_mirror != None and baseurl != None:
        fail("fedora_release takes rpmrepo_mirror/rpmrepo_snapshot or baseurl, not both: {}".format(name))
    repository_metadata = {}
    if rpmrepo_mirror != None:
        baseurl = rpmrepo_mirror.rstrip("/") + "/" + rpmrepo_snapshot

        # refresh-catalog reads the pin back through this metadata to advance the snapshot.
        repository_metadata = {"rpmrepo.mirror": rpmrepo_mirror, "rpmrepo.snapshot": rpmrepo_snapshot}
    elif baseurl == None:
        if version == "rawhide":
            baseurl = "https://dl.fedoraproject.org/pub/fedora/linux/development/rawhide/Everything/x86_64/os/"
        else:
            baseurl = "https://dl.fedoraproject.org/pub/fedora/linux/releases/{}/Everything/x86_64/os/".format(version)

    rpm_remote_repository(
        name = name + ".repository",
        baseurl = baseurl,
        metadata = repository_metadata,
    )
    repository_universe(
        name = name + ".repositories",
        package_system = _RPM,
        required_repositories = [":" + name + ".repository"],
    )
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

# buildifier: disable=function-docstring-args
def centos_stream_release(
        name: str,
        version: str,
        engine: str,
        repository_urls: dict[str, str] = {},
        package_set_overrides: dict[str, list[str]] = {},
        enable_repository_groups: list[str] = ["crb"],
        disable_repository_groups: list[str] = [],
        additional_repositories: list[str] = [],
        repository_priorities: dict[str, int] = {},
        visibility: list[str] | None = None) -> None:
    """Declare the conventional CentOS Stream release target bundle."""
    _check_name(name, "centos", version)
    components = {
        "baseos": "BaseOS",
        "appstream": "AppStream",
        "crb": "CRB",
    }
    unknown = [component for component in repository_urls if component not in components]
    if unknown:
        fail("centos_stream_release: unknown repository components {}".format(sorted(unknown)))
    for component, path in components.items():
        rpm_remote_repository(
            name = "{}.{}.repository".format(name, component),
            baseurl = repository_urls.get(
                component,
                "https://mirror.stream.centos.org/{}/{}/x86_64/os/".format(version, path),
            ),
        )

    repository_universe(
        name = name + ".repositories",
        package_system = _RPM,
        required_repositories = [":" + name + ".baseos.repository"],
        optional_repository_groups = {
            "appstream": [":" + name + ".appstream.repository"],
            "crb": [":" + name + ".crb.repository"],
        },
        default_repository_groups = ["appstream"],
    )
    os_release(
        name = name + ".release",
        repository_universe = ":" + name + ".repositories",
        package_sets = _merge_package_sets(_CENTOS_STREAM_PACKAGE_SETS, package_set_overrides),
        visibility = visibility,
    )
    package_manager(
        name = name + ".package-manager",
        release = ":" + name + ".release",
        engine = engine,
        enable_repository_groups = enable_repository_groups,
        disable_repository_groups = disable_repository_groups,
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
