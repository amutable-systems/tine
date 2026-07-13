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
        RunInfo(args = cmd_args(ctx.attrs.src)),
    ]

_tool = rule(
    impl = _tool_impl,
    attrs = {"src": attrs.source()},
)

# buildifier: disable=function-docstring-args
def http_tool(
        name: str,
        platforms: dict[str, dict[str, str]],
        path: str | None = None,
        visibility: list[str] | None = None) -> None:
    """Pin a raw binary or archive member for each supported CPU."""
    urls = select({_CPU_SETTING[cpu]: [e["url"]] for cpu, e in platforms.items()})
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
    _tool(name = name, src = src, visibility = visibility)
