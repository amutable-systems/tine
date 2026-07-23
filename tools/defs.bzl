"""`http_tool`: pin a prebuilt tool binary that buck downloads and sha256-verifies itself."""

# Explicit loads keep Starlark type checking precise.
load("@prelude//:rules.bzl", "http_archive", "http_file")

# Prelude CPU settings keyed by upstream architecture names.
_CPU_SETTING = {
    "aarch64": "config//cpu:arm64",
    "x86_64": "config//cpu:x86_64",
}

def _tool_impl(ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(default_output = ctx.attrs.src),
        RunInfo(args = cmd_args(ctx.attrs.src, ctx.attrs.args)),
    ]

_tool = rule(
    impl = _tool_impl,
    attrs = {
        "args": attrs.list(attrs.string(), default = []),
        "src": attrs.source(),
    },
)

# buildifier: disable=function-docstring-args
def http_tool(
        name: str,
        spec: dict,
        args: list[str] | None = None,
        path: str | None = None,
        visibility: list[str] | None = None) -> None:
    """Pin a raw binary or archive member for each supported CPU.

    `spec` is the tools.json entry: a `repository`, a `release` tag, and per-CPU `artifact` + `sha256`
    (+ optional `strip_prefix`). The download URL is derived from these.
    """
    base = "https://github.com/{}/releases/download/{}".format(spec["repository"], spec["release"])
    platforms = spec["platforms"]
    urls = select({_CPU_SETTING[cpu]: ["{}/{}".format(base, e["artifact"])] for cpu, e in platforms.items()})
    sha256 = select({_CPU_SETTING[cpu]: e["sha256"] for cpu, e in platforms.items()})
    if path == None:
        http_file(
            name = name + "-download",
            executable = True,
            sha256 = sha256,
            urls = urls,
        )
        src = ":{}-download".format(name)
    else:
        http_archive(
            name = name + "-download",
            sha256 = sha256,
            strip_prefix = select({_CPU_SETTING[cpu]: e.get("strip_prefix") for cpu, e in platforms.items()}),
            sub_targets = [path],
            urls = urls,
        )
        src = ":{}-download[{}]".format(name, path)
    _tool(name = name, args = args or [], src = src, visibility = visibility)
