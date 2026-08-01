"""Build project development environments and enter them interactively."""

load("//engine:build.bzl", "engine")
load("//engine:runtime.bzl", "EngineInfo", "chroot_run")

DEFAULT_RELEASE = "tine//catalog:fedora.rawhide.release"
DEFAULT_RESOLVER_ENGINE = "tine//catalog:fedora.rawhide.engine"

def _box_impl(ctx: AnalysisContext) -> list[Provider]:
    engine = ctx.attrs.engine[EngineInfo]
    return [
        DefaultInfo(default_output = engine.root),
        engine,
        chroot_run(engine, relaxed = True, box = ctx.label.name),
    ]

_box = rule(
    impl = _box_impl,
    attrs = {
        "engine": attrs.dep(providers = [EngineInfo]),
        "labels": attrs.list(attrs.string(), default = []),
    },
)

def box(
    name: str,
    packages: list[str],
    release: str = DEFAULT_RELEASE,
    resolver_engine: str = DEFAULT_RESOLVER_ENGINE,
    enable_repository_groups: list[str] = [],
    disable_repository_groups: list[str] = [],
    arch: str = "x86_64",
    labels: list[str] = [],
    visibility: list[str] | None = None,
) -> None:
    """Build an engine and expose its host-integrated interactive entry point."""
    if not packages:
        fail("box: packages must not be empty")
    engine(
        name = name + ".engine",
        packages = packages,
        release = release,
        resolver_engine = resolver_engine,
        enable_repository_groups = enable_repository_groups,
        disable_repository_groups = disable_repository_groups,
        arch = arch,
        visibility = visibility,
    )
    _box(
        name = name,
        engine = ":" + name + ".engine",
        labels = labels,
        visibility = visibility,
    )
