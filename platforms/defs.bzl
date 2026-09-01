"""tine's execution platform: local execution, optionally against a shared cache."""

load("@prelude//cfg/exec_platform:marker.bzl", "get_exec_platform_marker")

def _cache_configured() -> bool:
    """Whether `[buck2_re_client]` names a cache to talk to.

    `address` is buck's own fallback for `{engine,action_cache,cas}_address`, and `engine_address` is
    the one buck reaches first, for its capabilities query.
    """
    for key in ("address", "engine_address"):
        if read_root_config("buck2_re_client", key) != None:
            return True
    return False

def _execution_platform_impl(ctx: AnalysisContext) -> list[Provider]:
    constraints = dict()
    constraints.update(ctx.attrs.cpu_configuration[ConfigurationInfo].constraints)
    constraints.update(ctx.attrs.os_configuration[ConfigurationInfo].constraints)
    cfg = ConfigurationInfo(constraints = constraints, values = {})

    name = ctx.label.raw_target()
    platform = ExecutionPlatformInfo(
        label = name,
        configuration = cfg,
        executor_config = CommandExecutorConfig(
            # Actions run here, never on a remote worker: the sandbox unshares and mounts, and a VM test
            # wants the host's /dev/kvm. Only the cache is shared.
            local_enabled = True,
            remote_enabled = False,
            remote_cache_enabled = ctx.attrs.remote_cache_enabled,
            # actual uploads only happen for actions that opt in
            allow_cache_uploads = True,
            # guard against uploading unexpectedly large files; not policy (action's `allow_cache_upload` is)
            # needs to fit the biggest package/compiler output; we don't generally remote-cache image builds
            max_cache_upload_mebibytes = 10240,
        ),
    )

    return [
        DefaultInfo(),
        platform,
        PlatformInfo(label = str(name), configuration = cfg),
        ExecutionPlatformRegistrationInfo(
            platforms = [platform],
            exec_marker_constraint = get_exec_platform_marker(),
        ),
    ]

_execution_platform = rule(
    impl = _execution_platform_impl,
    attrs = {
        "cpu_configuration": attrs.dep(providers = [ConfigurationInfo]),
        "os_configuration": attrs.dep(providers = [ConfigurationInfo]),
        "remote_cache_enabled": attrs.bool(),
    },
)

def execution_platform(name: str, **kwargs) -> None:
    """Register the platform, reading the cache switch here rather than taking it from the caller.

    Buck refuses to build at all once an executor enables `remote_cache_enabled` without configuring
    one. So the cache decision happens via `[buck2_re_client]` config presence.
    """
    _execution_platform(name = name, remote_cache_enabled = _cache_configured(), **kwargs)
