"""The public Cargo API.

Load the `cargo` namespace from here; the modules behind it are implementation structure and may be
rearranged.
"""

load("//cargo:rules.bzl", "cargo_package")

cargo = struct(
    package = cargo_package,
)
