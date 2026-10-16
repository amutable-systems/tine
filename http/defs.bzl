# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public HTTP download API, exported as the `http` namespace."""

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
    providers = [DefaultInfo(default_output = src)]
    if not ctx.attrs.directory:
        providers.append(RunInfo(args = cmd_args(src, ctx.attrs.args)))
    elif ctx.attrs.path != None:
        # `buck run` needs the whole unpacked archive, because a program such as go reads the files beside it.
        providers.append(RunInfo(args = cmd_args(src.project(ctx.attrs.path), ctx.attrs.args, hidden = src)))
    return providers

_tool = rule(
    impl = _tool_impl,
    attrs = {
        "args": attrs.list(attrs.string(), default = []),
        "directory": attrs.bool(default = False),
        "path": attrs.option(attrs.string(), default = None),
        "src": attrs.source(allow_directory = True),
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
    directory: bool = False,
    visibility: list[str] | None = None,
) -> None:
    """Pin a prebuilt tool for each supported CPU, downloaded and sha256-verified by Buck.

    `spec` is a tine.lock.json entry: a `release`, and per-CPU `artifact`, `sha256` and `size` (+ optional
    `strip_prefix`). The artifact is downloaded from the GitHub release of the entry's `repository`, or from
    the entry's `url`. `path` selects a member of an archive; `compressed` decompresses a bare
    zstd-compressed binary, which Buck's http rules cannot unpack themselves. `directory` makes the whole
    unpacked archive the default output, and `path` then names the program that `buck run` runs.
    """
    if directory and compressed:
        fail("http.tool {}: a compressed download is a single binary, not a directory".format(name))
    base = spec["url"] if "url" in spec else "https://github.com/{}/releases/download/{}".format(spec["repository"], spec["release"])
    platforms = spec["platforms"]
    urls = architecture.select({cpu: ["{}/{}".format(base, e["artifact"])] for cpu, e in platforms.items()})
    sha256 = architecture.select({cpu: e["sha256"] for cpu, e in platforms.items()})

    # Without a size buck asks the server for one with a HEAD request before every download, which is
    # a second chance for a release host to fail a build that has the bytes pinned already.
    size_bytes = architecture.select({cpu: e["size"] for cpu, e in platforms.items()})
    if path == None and not directory:
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
            sub_targets = [] if directory else [path],
            urls = urls,
        )
        src = ":{}-download".format(name) if directory else ":{}-download[{}]".format(name, path)
    _tool(
        name = name,
        args = args or [],
        directory = directory,
        path = path if directory else None,
        src = src,
        unzstd = "tine//http:unzstd" if compressed else None,
        visibility = visibility,
    )

http = struct(
    tool = http_tool,
)
