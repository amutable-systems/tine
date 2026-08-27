"""The public Go API.

Load the `go` namespace from here; the modules behind it are implementation structure and may be
rearranged.
"""

load("//go:rules.bzl", "go_package")

go = struct(
    package = go_package,
)
