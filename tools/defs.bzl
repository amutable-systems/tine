def _dotslash_tool_impl(ctx: AnalysisContext) -> list[Provider]:
    # The DotSlash descriptor is executable (`#!/usr/bin/env dotslash`); running
    # it directly resolves the shebang and fetches/verifies the pinned binary.
    # Expose both a default output (the descriptor) and RunInfo (so it can back a
    # toolchain or be `$(exe ...)`'d).
    return [
        DefaultInfo(default_output = ctx.attrs.src),
        RunInfo(args = cmd_args(ctx.attrs.src)),
    ]

dotslash_tool = rule(
    impl = _dotslash_tool_impl,
    attrs = {"src": attrs.source()},
)
