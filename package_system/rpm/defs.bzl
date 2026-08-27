"""The public RPM package-system API, exported as the `rpm` namespace."""

load(
    "//package_system/rpm:rules.bzl",
    "rpm_package",
    "rpm_remote_repository",
)

rpm = struct(
    package = rpm_package,
    remote_repository = rpm_remote_repository,
)
