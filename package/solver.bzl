"""Construct package-solver commands and reusable repository caches."""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load(":repository.bzl", "ConfiguredPackageRepositoryInfo", "write_repository_manifest")
load(":system.bzl", "PackageSystemInfo")

# buildifier: disable=function-docstring-args
# buildifier: disable=function-docstring-return
def solve_command(
        ctx: AnalysisContext,
        engine: EngineInfo,
        system: PackageSystemInfo,
        repositories: list[ConfiguredPackageRepositoryInfo],
        install: list[str],
        arch: str,
        output: OutputArtifact | None = None,
        solver_caches: list[Artifact] = [],
        lowers: list[Artifact] = [],
        manifest_name: str = "repositories.json") -> cmd_args:
    """Construct a package solve command for one configured solver context."""
    command = cmd_args(
        chroot_run(engine = engine, exe = system.plan),
        "solve",
        "--arch",
        arch,
    )
    if output != None:
        command.add("--out", output)
    for cache in solver_caches:
        command.add("--cache", cache)
    command.add("--repositories", write_repository_manifest(ctx, manifest_name, repositories))
    for lower in lowers:
        command.add("--lower", lower)
    for package in install:
        command.add("--install", package)
    return command

def _solver_cache_impl(ctx: AnalysisContext) -> list[Provider]:
    engine = ctx.attrs.engine[EngineInfo]
    system = ctx.attrs.package_system[PackageSystemInfo]
    repository = ConfiguredPackageRepositoryInfo(
        id = ctx.attrs.repository_id,
        directory = ctx.attrs.repository_dir,
        priority = ctx.attrs.priority,
        baseurl = ctx.attrs.baseurl,
    )
    cache = ctx.actions.declare_output("cache", dir = True)
    manifest = write_repository_manifest(ctx, "repositories.json", [repository])
    ctx.actions.run(
        cmd_args(
            chroot_run(engine = engine, exe = system.plan),
            "make-cache",
            "--arch",
            ctx.attrs.arch,
            "--repositories",
            manifest,
            "--out",
            cache.as_output(),
        ),
        category = "solver_cache",
        identifier = repository.id,
    )
    return [DefaultInfo(default_output = cache)]

_solver_cache = anon_rule(
    impl = _solver_cache_impl,
    attrs = {
        "arch": attrs.string(),
        "baseurl": attrs.option(attrs.string(), default = None),
        "engine": attrs.dep(providers = [EngineInfo]),
        "package_system": attrs.dep(providers = [PackageSystemInfo]),
        "priority": attrs.int(),
        "repository_dir": attrs.source(),
        "repository_id": attrs.string(),
    },
    artifact_promise_mappings = {
        "cache": lambda providers: providers[DefaultInfo].default_outputs[0],
    },
)

def solver_cache(
        ctx: AnalysisContext,
        engine: Dependency,
        package_system: Dependency,
        repository: ConfiguredPackageRepositoryInfo,
        arch: str) -> Artifact:
    """Reuse parsed repository metadata for an identical solver context."""
    return ctx.actions.anon_target(_solver_cache, {
        "arch": arch,
        "baseurl": repository.baseurl,
        "engine": engine,
        "package_system": package_system,
        "priority": repository.priority,
        "repository_dir": repository.directory,
        "repository_id": repository.id,
    }).artifact("cache")
