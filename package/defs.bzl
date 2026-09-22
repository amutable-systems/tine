# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public package API.

Load the `package` namespace from here; the modules behind it are implementation structure and may
be rearranged.
"""

load(
    "//package:manager.bzl",
    "PackageManagerInfo",
    "package_manager",
)
load("//package:repository.bzl", "local_repository")

package = struct(
    ManagerInfo = PackageManagerInfo,
    local_repository = local_repository,
    manager = package_manager,
)
