# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The conventional Debian distribution declaration."""

load("//distribution:defs.bzl", "distribution")
load("//package:manager.bzl", "package_manager")
load("//package:release.bzl", "os_release")
load("//package:repository.bzl", "repository_universe")
load("//package_system/deb:rules.bzl", "ARCHIVE_MIRROR", "PACKAGE_SYSTEM", "deb_remote_repository")
load("//platforms:architecture.bzl", "architecture")

# The archive's signing keys, named as ftp-master serves them, e.g.:
#   gpg --show-keys --with-fingerprint archive-key-13.asc
# cross-check the fingerprints against the debian-archive-keyring package when adding one
_DEBIAN_KEY_URL = "https://ftp-master.debian.org/keys/{}.asc"
_DEBIAN_SIGNING_KEYS = {
    "archive-key-12": "B8B80B5B623EAB6AD8775C45B7C5D7D6350947F8",
    "archive-key-13": "04B54C3CDCA79751B16BC6B5225629DF75B188BD",
}

def debian_signing_keys(*names) -> dict[str, str]:
    """The `signing_keys` for repositories whose Release one of the named archive keys signs."""
    for name in names:
        if name not in _DEBIAN_SIGNING_KEYS:
            fail("no Debian signing key {} is known; add its fingerprint to distribution/debian.bzl".format(name))
    return {_DEBIAN_SIGNING_KEYS[name]: _DEBIAN_KEY_URL.format(name) for name in names}

def _package_sets(arch: str) -> dict[str, list[str]]:
    """The conventional sets, with the one entry that is named for an architecture."""
    sets = dict(_DEBIAN_PACKAGE_SETS)
    sets["bootable"] = sorted(sets["bootable"] + ["linux-image-{}".format(arch)])
    return sets

_DEBIAN_PACKAGE_SETS = {
    "bootable": [
        "bash",
        "coreutils",
        "dbus-broker",
        # systemd dlopens libfdisk for repart and the TPM2 stack for measured boot and unlock, so
        # nothing in Debian's metadata declares either as a dependency. Debian names a tpm2-tss
        # library after its soname, so a soname bump renames these and the solve stops finding
        # them; `apt-cache search libtss2-esys` in the box names the successor.
        "libfdisk1",
        "libtss2-esys-3.0.2-0t64",
        "libtss2-mu-4.0.1-0t64",
        "libtss2-rc0t64",
        "libtss2-tcti-device0t64",
        "login",
        "systemd",
        "systemd-boot-efi",
        "systemd-boot-tools",
        "systemd-sysv",
        "udev",
        "util-linux",
    ],
    # `build-essential` is Debian's name for the assumed build environment.
    "buildroot": ["build-essential"],
    "initrd": [
        "bash",
        "cryptsetup-bin",
        "kmod",
        "libtss2-esys-3.0.2-0t64",
        "libtss2-mu-4.0.1-0t64",
        "libtss2-rc0t64",
        "libtss2-tcti-device0t64",
        "mount",
        "systemd",
        "systemd-cryptsetup",
        "udev",
        "util-linux",
    ],
}

_COMPONENTS = ("main", "contrib", "non-free-firmware", "non-free")
_REQUIRED = "main"

def _check_name(name: str) -> None:
    if not name.startswith("debian.") or name.count(".") != 1:
        fail("debian release name must be 'debian.<suite>': {}".format(name))

def debian_release(
    name: str,
    box: str,
    architectures: list[str],
    signing_keys: dict[str, str],
    suite: str | None = None,
    archive_snapshot: str | None = None,
    archive_mirror: str = ARCHIVE_MIRROR,
    repository_urls: dict[str, str] = {},
    package_set_overrides: dict[str, list[str]] = {},
    enable_repository_groups: list[str] = [],
    disable_repository_groups: list[str] = [],
    additional_repositories: list[str] = [],
    repository_priorities: dict[str, int] = {},
    visibility: list[str] | None = None,
) -> None:
    """Declare the conventional Debian release target bundle.

    `signing_keys` are the archive keys one of which must have signed the suite's Release, normally
    `debian_signing_keys(...)`; they are stated so that a reviewer sees which keys a release trusts.

    Every component shares one archive pin, so the release stays internally consistent: main and
    contrib are only guaranteed to solve together when they come from the same timestamp.
    Overriding the mirrors is therefore all or nothing, since a release with one component off the
    pin would have refresh-catalog advance its siblings past it.
    """
    _check_name(name)
    suite = suite or name.removeprefix("debian.")
    unknown = [component for component in repository_urls if component not in _COMPONENTS]
    if unknown:
        fail("debian_release: unknown repository components {}".format(sorted(unknown)))
    if (archive_snapshot == None) == (not repository_urls):
        fail("debian_release: takes archive_snapshot or repository_urls, not both or neither: {}".format(name))
    if repository_urls and sorted(repository_urls) != sorted(_COMPONENTS):
        fail(
            "debian_release: repository_urls must cover every component {}: {}".format(
                sorted(_COMPONENTS),
                name,
            ),
        )

    for component in _COMPONENTS:
        deb_remote_repository(
            name = "{}.{}.repository".format(name, component),
            suite = suite,
            component = component,
            architectures = architectures,
            baseurl = repository_urls.get(component),
            archive_mirror = archive_mirror if archive_snapshot else None,
            archive_snapshot = archive_snapshot,
            signing_keys = signing_keys,
        )

    repository_universe(
        name = name + ".repositories",
        package_system = PACKAGE_SYSTEM,
        required_repositories = [":{}.{}.repository".format(name, _REQUIRED)],
        optional_repository_groups = {component: [":{}.{}.repository".format(name, component)] for component in _COMPONENTS if component != _REQUIRED},
        default_repository_groups = ["non-free-firmware"],
    )
    distribution.new(name = name + ".distribution", visibility = visibility)
    package_sets = _package_sets(architecture.spelling(architectures[0], "deb"))
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
