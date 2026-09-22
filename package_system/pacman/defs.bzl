# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public alpm package-system API, exported as the `pacman` namespace."""

load("//package_system/pacman:rules.bzl", "pacman_remote_repository")

pacman = struct(
    remote_repository = pacman_remote_repository,
)
