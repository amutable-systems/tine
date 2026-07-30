"""Represent reusable execution environments and run commands inside them."""

ASSEMBLY_SDE = 1739577600

EngineInfo = provider(
    # Carry the configured sandbox through providers so anonymous targets can reuse it.
    doc = "A reusable execution environment built from one base OS release.",
    fields = {
        "arch": provider_field(str),
        "root": provider_field(Artifact),  # the engine root chroot
        "sandbox": provider_field(Dependency),
    },
)

def chroot_run(
    engine: EngineInfo,
    exe: Dependency | str | None = None,
    network: bool = False,
    relaxed: bool = False,
) -> RunInfo:
    """Enter an engine, optionally running a command or interactive relaxed leaf."""
    run = cmd_args(
        engine.sandbox[RunInfo],
        "--tools",
        engine.root,
    )
    if relaxed:
        run.add("--relaxed")
    else:
        run.add("--bind-cwd", "--source-date-epoch", str(ASSEMBLY_SDE))
    if network:
        run.add("--network")
    run.add("--")
    if isinstance(exe, Dependency):
        info = exe[DefaultInfo]
        run.add(cmd_args(info.default_outputs[0], hidden = info.other_outputs))
    elif exe != None:
        run.add(exe)
    return RunInfo(args = run)
