# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Shared tine Python support, and the toolchain Buck2 looks up as `toolchains//`."""

load("@prelude//toolchains:python.bzl", "python_bootstrap_toolchain")
load("//box:test.bzl", "box_python_test")
load("//python:defs.bzl", "tine_python_library")

# Declared here rather than in a `toolchains` cell of its own: Buck2 forbids nesting a cell inside an
# external cell, so declaring it here is what lets a consuming project pull tine in as one.
python_bootstrap_toolchain(
    name = "python_bootstrap",
    interpreter = read_config("tine", "python_interpreter", "tine//tools:python3"),
    visibility = ["PUBLIC"],
)

export_file(
    name = "ty-config",
    src = "ty.toml",
    visibility = ["PUBLIC"],
)

# bump rewrites tine.lock.json in the source tree. The default mode copies the file to buck-out, and
# bump would rewrite the copy.
export_file(
    name = "tine.lock.json",
    src = "tine.lock.json",
    mode = "reference",
    visibility = ["//tools/..."],
)

export_file(
    name = "buckconfig",
    src = ".buckconfig",
    out = "tine.buckconfig",
    visibility = ["//bin/..."],
)

tine_python_library(
    name = "util",
    srcs = ["util.py"],
    visibility = ["PUBLIC"],
)

box_python_test(
    name = "test",
    box = "//tools:dev.box",
    srcs = ["util_test.py"],
    deps = [":util"],
)

tine_python_library(
    name = "specs",
    srcs = ["specs.py"],
    visibility = ["PUBLIC"],
)
