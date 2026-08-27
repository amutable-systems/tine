"""The public package API.

Load the `package` namespace from here; the modules behind it are implementation structure and may
be rearranged.
"""

load(
    "//package:manager.bzl",
    "PackageManagerInfo",
    "package_manager",
)

package = struct(
    ManagerInfo = PackageManagerInfo,
    manager = package_manager,
)
