"""The public RPM API.

Declare a release, the repositories behind it, and a package built from a spec; the modules behind
this are implementation structure and may be rearranged.
"""

load("//package_system/rpm:catalog.bzl", _fedora_release = "fedora_release")
load(
    "//package_system/rpm:rules.bzl",
    _rpm_package = "rpm_package",
    _rpm_remote_repository = "rpm_remote_repository",
)

fedora_release = _fedora_release
rpm_remote_repository = _rpm_remote_repository
rpm_package = _rpm_package
