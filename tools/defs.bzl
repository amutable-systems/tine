"""`http_tool`: pin a prebuilt tool binary that buck downloads and sha256-verifies itself."""

# Explicit loads (rather than the implicit `native.…`) so `buck starlark typecheck` can
# resolve the rules.
load("@prelude//:rules.bzl", "http_archive", "http_file")

# The prelude's cpu config settings, keyed by the arch names upstream releases use.
_CPU_SETTING = {
    "aarch64": "config//cpu:arm64",
    "x86_64": "config//cpu:x86_64",
}

def _tool_impl(ctx: AnalysisContext) -> list[Provider]:
    # Forward the fetched binary as the default output (so `buck build
    # --show-full-simple-output` resolves it to a path) and expose RunInfo (so it
    # can back a toolchain or be `$(exe ...)`'d).
    return [
        DefaultInfo(default_output = ctx.attrs.src),
        RunInfo(args = cmd_args(ctx.attrs.src)),
    ]

_tool = rule(
    impl = _tool_impl,
    attrs = {"src": attrs.source()},
)

def http_tool(name, platforms, path = None, visibility = None):
    """Pin a prebuilt tool binary fetched + sha256-verified by buck on first use.

    `platforms` maps a cpu (`x86_64`/`aarch64`) to `{url, sha256[, strip_prefix]}`. With `path`,
    the url is an archive holding the binary at `path` (after `strip_prefix`); without, the url is
    the raw binary itself.
    """
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
