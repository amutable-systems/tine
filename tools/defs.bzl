# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""`http_tool`: pin a prebuilt tool binary that buck downloads and sha256-verifies itself."""

# Explicit loads keep Starlark type checking precise.
load("@prelude//:rules.bzl", "http_archive", "http_file")
load("//platforms:architecture.bzl", "architecture")

def _tool_impl(ctx: AnalysisContext) -> list[Provider]:
    src = ctx.attrs.src
    if ctx.attrs.unzstd != None:
        out = ctx.actions.declare_output(ctx.label.name)
        ctx.actions.run(
            cmd_args(ctx.attrs.unzstd[RunInfo], src, out.as_output()),
            category = "unzstd",
        )
        src = out
    return [
        DefaultInfo(default_output = src),
        RunInfo(args = cmd_args(src, ctx.attrs.args)),
    ]

_tool = rule(
    impl = _tool_impl,
    attrs = {
        "args": attrs.list(attrs.string(), default = []),
        "src": attrs.source(),
        # Only set for compressed downloads: an unconditional dep would cycle, since the decompressor
        # is a tine_python_binary and the bootstrap interpreter is itself an http_tool.
        "unzstd": attrs.option(attrs.exec_dep(providers = [RunInfo]), default = None),
    },
)

def http_tool(
    name: str,
    spec: dict,
    args: list[str] | None = None,
    path: str | None = None,
    compressed: bool = False,
    visibility: list[str] | None = None,
) -> None:
    """Pin a raw binary or archive member for each supported CPU.

    `spec` is the tools.json entry: a `repository`, a `release` tag, and per-CPU `artifact`, `sha256`
    and `size` (+ optional `strip_prefix`). The download URL is derived from these. `path` selects a
    member of an archive; `compressed` decompresses a bare zstd-compressed binary, which Buck's http
    rules cannot unpack themselves.
    """
    base = "https://github.com/{}/releases/download/{}".format(spec["repository"], spec["release"])
    platforms = spec["platforms"]
    urls = architecture.select({cpu: ["{}/{}".format(base, e["artifact"])] for cpu, e in platforms.items()})
    sha256 = architecture.select({cpu: e["sha256"] for cpu, e in platforms.items()})

    # Without a size buck asks the server for one with a HEAD request before every download, which is
    # a second chance for a release host to fail a build that has the bytes pinned already.
    size_bytes = architecture.select({cpu: e["size"] for cpu, e in platforms.items()})
    if path == None:
        http_file(
            name = name + "-download",
            executable = True,
            sha256 = sha256,
            size_bytes = size_bytes,
            urls = urls,
        )
        src = ":{}-download".format(name)
    else:
        http_archive(
            name = name + "-download",
            sha256 = sha256,
            size_bytes = size_bytes,
            strip_prefix = architecture.select({cpu: e.get("strip_prefix") for cpu, e in platforms.items()}),
            sub_targets = [path],
            urls = urls,
        )
        src = ":{}-download[{}]".format(name, path)
    _tool(
        name = name,
        args = args or [],
        src = src,
        unzstd = "tine//tools:unzstd" if compressed else None,
        visibility = visibility,
    )
