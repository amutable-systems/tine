"""Shared tine Python support."""

python_bootstrap_library(
    name = "util",
    srcs = ["util.py"],
    visibility = ["PUBLIC"],
)

python_bootstrap_library(
    name = "specs",
    srcs = ["specs.py"],
    visibility = ["PUBLIC"],
)

# Spec loading and shared helpers for driver tests running in the unit-test tree
# (tine//tests:unit-test).
export_file(
    name = "specs-src",
    src = "specs.py",
    visibility = ["tine//tests:unit-test"],
)

export_file(
    name = "util-src",
    src = "util.py",
    visibility = ["tine//tests:unit-test"],
)
