# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""tine's platforms: where actions run, and which architecture they build for.

The execution platform is local, optionally against a shared cache. A target platform names constraints
and registers no executor: every action still runs on the local machine, so executed build tools have to
be reached as `exec_dep`. They cannot execute target platform code.
"""

load("@prelude//cfg/exec_platform:marker.bzl", "get_exec_platform_marker")

def _target_platform_impl(ctx: AnalysisContext) -> list[Provider]:
    constraints = dict()
    for configuration in ctx.attrs.configurations:
        constraints.update(configuration[ConfigurationInfo].constraints)

    # A target that has to be built for one architecture whatever asked for it takes this as its
    # incoming transition, rather than the whole build being pointed at the platform.
    def select_platform(platform: PlatformInfo) -> PlatformInfo:
        return PlatformInfo(
            label = platform.label,
            configuration = ConfigurationInfo(
                constraints = platform.configuration.constraints | constraints,
                values = platform.configuration.values,
            ),
        )

    return [
        DefaultInfo(),
        PlatformInfo(
            label = str(ctx.label.raw_target()),
            configuration = ConfigurationInfo(constraints = constraints, values = {}),
        ),
        TransitionInfo(impl = select_platform),
    ]

target_platform = rule(
    impl = _target_platform_impl,
    attrs = {
        "configurations": attrs.list(attrs.dep(providers = [ConfigurationInfo])),
    },
    is_configuration_rule = True,
)

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
            # only a cache that takes uploads, and then only for actions that opt in
            allow_cache_uploads = ctx.attrs.allow_cache_uploads,
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
        "allow_cache_uploads": attrs.bool(),
        "cpu_configuration": attrs.dep(providers = [ConfigurationInfo]),
        "os_configuration": attrs.dep(providers = [ConfigurationInfo]),
        "remote_cache_enabled": attrs.bool(),
    },
)

def execution_platform(name: str, **kwargs) -> None:
    """Register the platform, reading the cache switches here rather than taking them from the caller.

    Buck refuses to build at all once an executor enables `remote_cache_enabled` without configuring
    one. So the cache decision happens via `[buck2_re_client]` config presence.
    """
    _execution_platform(name = name, allow_cache_uploads = read_root_config("tine", "cache-uploads") == "true", remote_cache_enabled = _cache_configured(), **kwargs)
