# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public deb package-system API, exported as the `deb` namespace."""

load("//package_system/deb:rules.bzl", "deb_remote_repository")

deb = struct(
    remote_repository = deb_remote_repository,
)
