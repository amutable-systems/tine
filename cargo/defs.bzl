# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public Cargo API.

Load the `cargo` namespace from here; the modules behind it are implementation structure and may be
rearranged.
"""

load("//cargo:rules.bzl", "cargo_package")

cargo = struct(
    package = cargo_package,
)
