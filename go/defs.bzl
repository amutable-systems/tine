"""The public Go API.

Load the Go project rule from here; the modules behind it are implementation structure and may be
rearranged.
"""

load("//go:rules.bzl", _go_package = "go_package")

go_package = _go_package
