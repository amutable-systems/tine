"""Shared actions for materializing logical image stacks."""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")

def stack_command(engine: Dependency, layers: list[Artifact], driver: Dependency) -> cmd_args:
    """Build a `driver --lower <delta>...` command that reads an image's delta stack."""
    cmd = cmd_args(chroot_run(engine = engine[EngineInfo], exe = driver))
    for lower in layers:
        cmd.add("--lower", lower)
    return cmd

def archive_action(
        ctx: AnalysisContext,
        engine: Dependency,
        layers: list[Artifact],
        tmpfiles: list[str],
        driver: Dependency,
        format: str,
        out: OutputArtifact) -> None:
    """Materialize an image stack in an archive driver's output format."""
    cmd = cmd_args(
        chroot_run(engine = engine[EngineInfo], exe = driver),
        "--out",
        out,
        "--format",
        format,
    )
    for lower in layers:
        cmd.add("--lower", lower)
    for snippet in tmpfiles:
        cmd.add("--tmpfiles", snippet)
    ctx.actions.run(cmd, category = "image_" + format)
