"""The public test API, exported as the `tests` namespace."""

load("@tine//distribution:defs.bzl", "distribution")

def _vm_smoke_impl(ctx: AnalysisContext) -> list[Provider]:
    command = cmd_args(
        ctx.attrs._driver[RunInfo],
        cmd_args(ctx.attrs.checks, format = "--check={}"),
        # The runner is a command of its own, carrying options this one would otherwise try to
        # parse; it is also data for the driver rather than a host tool, so it stays in the target
        # configuration and reuses the image a build of that target already produced.
        "--",
        ctx.attrs.vm[RunInfo],
    )
    return [
        DefaultInfo(),
        # `buck run` on a smoke boots it interactively, which is how a failure is investigated.
        RunInfo(args = command),
        ExternalRunnerTestInfo(
            type = "custom",
            command = [command],
            # `image` is every test that needs an example image built, which is minutes rather
            # than seconds; `vm` is the ones that additionally boot one.
            labels = ["image", "vm"] + ctx.attrs.labels,
            # A VM needs the host's /dev/kvm, so it cannot be shipped to a remote executor.
            default_executor = CommandExecutorConfig(local_enabled = True, remote_enabled = False),
        ),
    ]

_vm_smoke = rule(
    doc = """Boot one `image_vm` runner, assert each check succeeds in the guest, then power off.

    A check is an ordinary shell command run in the booted guest; the smoke fails naming the first
    that does not succeed.
    """,
    impl = _vm_smoke_impl,
    attrs = {
        "checks": attrs.list(attrs.string(), doc = "shell commands that must succeed in the guest"),
        "labels": attrs.list(attrs.string(), default = []),
        "vm": attrs.dep(providers = [RunInfo], doc = "the image_vm runner to boot"),
        "_driver": attrs.exec_dep(providers = [RunInfo], default = "tine//tests:boot-smoke"),
    },
    supports_incoming_transition = True,
)

def vm_smoke(name: str, distro: str | None = None, visibility: list[str] | None = None, **kwargs) -> None:
    """Declare one, compatible with the distributions its package serves."""
    _vm_smoke(name = name, **(distribution.distributed(name, distro, visibility) | kwargs))

tests = struct(
    vm_smoke = vm_smoke,
)
