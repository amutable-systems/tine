"""The public box API.

Declare a box here, enter one from a rule, and run a test suite inside one; the modules behind
this are implementation structure and may be rearranged.
"""

load("//box:build.bzl", _box = "box")
load("//box:runtime.bzl", _BoxInfo = "BoxInfo", _box_run = "box_run")
load("//box:test.bzl", _box_python_test = "box_python_test", _box_sh_test = "box_sh_test")

box = _box
box_run = _box_run
BoxInfo = _BoxInfo
box_python_test = _box_python_test
box_sh_test = _box_sh_test
