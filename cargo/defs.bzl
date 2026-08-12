"""The public Cargo API.

Load the Rust project rule from here; the modules behind it are implementation structure and may
be rearranged.
"""

load("//cargo:rules.bzl", _cargo_package = "cargo_package")

cargo_package = _cargo_package
