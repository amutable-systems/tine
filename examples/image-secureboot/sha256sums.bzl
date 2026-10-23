# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The author hook of the example release, which runs its driver in a box like any build action."""

load("@tine//box:defs.bzl", "box")

def _sha256sums_hook_impl(ctx: AnalysisContext) -> list[Provider]:
    return [DefaultInfo(), box.run(box = ctx.attrs.box[box.Info], exe = ctx.attrs.driver)]

sha256sums_hook = rule(
    impl = _sha256sums_hook_impl,
    attrs = {
        "box": attrs.exec_dep(providers = [box.Info]),
        "driver": attrs.exec_dep(),
    },
)
