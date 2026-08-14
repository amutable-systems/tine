"""Shared tine Python support."""

load("//python:defs.bzl", "tine_python_library")

export_file(
    name = "ty-config",
    src = "ty.toml",
    visibility = ["PUBLIC"],
)

tine_python_library(
    name = "util",
    srcs = ["util.py"],
    visibility = ["PUBLIC"],
)

tine_python_library(
    name = "specs",
    srcs = ["specs.py"],
    visibility = ["PUBLIC"],
)
