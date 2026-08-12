"""The public package API.

Load the package manager a release is installed with from here; the modules behind it are
implementation structure and may be rearranged.
"""

load(
    "//package:manager.bzl",
    _PackageManagerInfo = "PackageManagerInfo",
    _package_manager = "package_manager",
)

package_manager = _package_manager
PackageManagerInfo = _PackageManagerInfo
