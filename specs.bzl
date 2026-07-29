"""Hand a driver its whole invocation as one JSON spec."""

def spec_args(
        ctx: AnalysisContext,
        name: str,
        spec: dict[str, typing.Any]) -> cmd_args:
    """Serialize one action's inputs, outputs, and configuration for its driver.

    Artifacts survive as their action paths, so a spec names declared outputs as readily as
    inputs and neither has to be repeated on the command line.
    """
    return cmd_args(
        "--spec",
        ctx.actions.write_json(
            name,
            spec,
            with_inputs = True,
            has_content_based_path = False,
        ),
    )

def spec_argument(argument: typing.Any) -> cmd_args:
    """Keep a resolved `$(location)` macro or executable a single string in a spec.

    write_json renders a command line as a list unless it is concatenated into one argument.
    """
    return cmd_args(argument, delimiter = "")

def executable(exe: Dependency) -> cmd_args:
    """Name an executable in a spec, keeping its runtime files as action inputs."""
    info = exe[DefaultInfo]
    return cmd_args(info.default_outputs[0], delimiter = "", hidden = info.other_outputs)
