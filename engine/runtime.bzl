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
    box: str | None = None,
    ro_binds: dict[str, str] = {},
    setenv: dict[str, str] = {},
) -> RunInfo:
    """Enter an engine, optionally running a command or interactive relaxed leaf.

    ro_binds maps a host path to where it appears inside, for the rare action that must reach host
    state; setenv adds to the sandbox's otherwise fixed environment. Both are for non-hermetic
    actions such as signing against an externally held key, never for build inputs.
    """
    run = cmd_args(
        engine.sandbox[RunInfo],
        "--tools",
        engine.root,
    )
    if relaxed:
        run.add("--relaxed")
    else:
        run.add("--bind-cwd", "--source-date-epoch", str(ASSEMBLY_SDE))
    if box != None:
        run.add("--box", box)
    if network:
        run.add("--network")

    # The sandbox creates each bind's mount point, which only works where the parent is writable, so a
    # destination belongs under the /run tmpfs rather than under the engine's read-only root.
    # Sort dict args as usual to retain stable command lines for buck input caching.
    for source in sorted(ro_binds):
        destination = ro_binds[source]
        for path in (source, destination):
            if ":" in path:
                fail("chroot_run: ro_binds path cannot contain ':', got {!r}".format(path))
        run.add("--ro-bind", "{}:{}".format(source, destination))
    for name in sorted(setenv):
        run.add("--setenv", "{}={}".format(name, setenv[name]))
    run.add("--")
    if isinstance(exe, Dependency):
        info = exe[DefaultInfo]
        run.add(cmd_args(info.default_outputs[0], hidden = info.other_outputs))
    elif exe != None:
        run.add(exe)
    return RunInfo(args = run)
