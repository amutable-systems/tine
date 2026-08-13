"""The public RPM package-system API."""

load(
    "//package_system/rpm:rules.bzl",
    _rpm_package = "rpm_package",
    _rpm_remote_repository = "rpm_remote_repository",
)

rpm_remote_repository = _rpm_remote_repository
rpm_package = _rpm_package
