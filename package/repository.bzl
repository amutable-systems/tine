"""Native package repositories, universes, and dynamic transaction selection."""

load(":system.bzl", "PackageSystemInfo")

PackageRepresentationInfo = record(
    artifact = Artifact,
    suffix = str,
)

PackageArtifactInfo = record(
    artifact = Artifact,
    name = str,
    representations = dict[str, PackageRepresentationInfo],
)

PackagePoolInfo = provider(
    doc = "A repository-owned dynamic package pool.",
    fields = {"value": provider_field(DynamicValue)},
)

PackagePoolValueInfo = provider(
    doc = "A resolved authoritative package pool keyed by stable package id.",
    fields = {"packages": provider_field(dict[str, PackageArtifactInfo])},
)

PackageRepositoryInfo = provider(
    doc = "A repository belonging to one native package system.",
    fields = {
        "baseurl": provider_field(str | None, default = None),
        # Local declarations are materialized by a consuming package manager.
        "dir": provider_field(Artifact | None, default = None),
        "package_system": provider_field(Dependency),
    },
)

ConfiguredPackageRepositoryInfo = record(
    id = str,
    dependency = field(Dependency | None, default = None),
    directory = Artifact,
    priority = int,
    baseurl = field(str | None, default = None),
)

LocalPackageInfo = provider(
    doc = "Built native packages and their native package system.",
    fields = {
        "package_system": provider_field(Dependency),
        "packages": provider_field(Artifact),
    },
)

LocalPackageRepositoryInfo = provider(
    doc = "A repository declaration backed by locally built package artifacts.",
    fields = {"package_dirs": provider_field(list[Artifact])},
)

def _local_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    packages = ctx.attrs.packages
    if not packages:
        fail("local_repository: packages must not be empty")
    context = packages[0][LocalPackageInfo]
    package_dirs = []
    for package in packages:
        info = package[LocalPackageInfo]
        if info.package_system.label != context.package_system.label:
            fail(
                "local_repository: package {} uses package system {}, expected {}".format(
                    package.label,
                    info.package_system.label,
                    context.package_system.label,
                ),
            )
        package_dirs.append(info.packages)

    return [
        DefaultInfo(),
        PackageRepositoryInfo(
            package_system = context.package_system,
        ),
        LocalPackageRepositoryInfo(
            package_dirs = package_dirs,
        ),
    ]

_local_repository = rule(
    impl = _local_repository_impl,
    attrs = {
        "packages": attrs.list(
            attrs.dep(providers = [LocalPackageInfo]),
            doc = "built native packages to publish",
        ),
    },
)

def local_repository(name: str, **kwargs) -> None:
    """Declare locally built packages as a repository for install operations."""
    if not name.endswith(".repository"):
        fail("local_repository name must end with '.repository': {}".format(name))
    _local_repository(name = name, **kwargs)

RepositoryUniverseInfo = provider(
    doc = "A homogeneous repository universe and its default selection policy.",
    fields = {
        "default_repository_groups": provider_field(list[str]),
        "optional_repository_groups": provider_field(dict[str, list[Dependency]]),
        "package_system": provider_field(Dependency),
        "required_repositories": provider_field(list[Dependency]),
    },
)

def _add_repository(
    package_system: Dependency,
    repository: Dependency,
    repositories: list[Dependency],
    by_id: dict[str, Dependency],
) -> None:
    repo = repository[PackageRepositoryInfo]
    rid = repository.label.name
    if repo.package_system.label != package_system.label:
        fail(
            "repository '{}' uses package system {}, expected {}".format(
                rid,
                repo.package_system.label,
                package_system.label,
            ),
        )
    previous = by_id.get(rid)
    if previous != None:
        if previous.label != repository.label:
            fail("repository id '{}' is provided by both {} and {}".format(rid, previous.label, repository.label))
        return
    by_id[rid] = repository
    repositories.append(repository)

def merge_repositories(package_system: Dependency, candidates: list[Dependency]) -> list[Dependency]:
    """Validate and de-duplicate repositories while preserving declaration order."""
    repositories = []
    by_id = {}

    for repository in candidates:
        _add_repository(package_system, repository, repositories, by_id)

    return repositories

def select_repositories(
    universe: Dependency,
    enable_repository_groups: list[str],
    disable_repository_groups: list[str],
) -> list[Dependency]:
    """Resolve a repository universe's required, default, and requested groups."""
    info = universe[RepositoryUniverseInfo]
    disabled = {name: True for name in disable_repository_groups}
    for name in disabled:
        if name not in info.default_repository_groups:
            fail("repository group '{}' is not enabled by default".format(name))

    group_names = [name for name in info.default_repository_groups if name not in disabled]
    for name in enable_repository_groups:
        if name not in info.optional_repository_groups:
            fail("unknown repository group '{}'".format(name))
        if name not in group_names:
            group_names.append(name)

    candidates = list(info.required_repositories)
    for name in group_names:
        candidates.extend(info.optional_repository_groups[name])
    return merge_repositories(info.package_system, candidates)

def encode_repositories(repositories: list[ConfiguredPackageRepositoryInfo]) -> list[dict[str, typing.Any]]:
    """Describe configured repositories for a driver spec.

    Dependency is analysis-only and cannot be serialized, so it stays out of the encoding.
    """
    return [
        {
            "baseurl": repository.baseurl,
            "directory": repository.directory,
            "id": repository.id,
            "priority": repository.priority,
        }
        for repository in repositories
    ]

def _repository_universe_impl(ctx: AnalysisContext) -> list[Provider]:
    candidates = list(ctx.attrs.required_repositories)
    for repositories in ctx.attrs.optional_repository_groups.values():
        candidates.extend(repositories)
    merge_repositories(ctx.attrs.package_system, candidates)

    seen = {}
    for name in ctx.attrs.default_repository_groups:
        if name in seen:
            fail("default repository group '{}' is listed twice".format(name))
        if name not in ctx.attrs.optional_repository_groups:
            fail("default repository group '{}' is not declared".format(name))
        seen[name] = True

    return [
        DefaultInfo(),
        RepositoryUniverseInfo(
            package_system = ctx.attrs.package_system,
            required_repositories = ctx.attrs.required_repositories,
            optional_repository_groups = ctx.attrs.optional_repository_groups,
            default_repository_groups = ctx.attrs.default_repository_groups,
        ),
    ]

_repository_universe = rule(
    impl = _repository_universe_impl,
    attrs = {
        "default_repository_groups": attrs.list(attrs.string(), default = []),
        "optional_repository_groups": attrs.dict(
            attrs.string(),
            attrs.list(attrs.dep(providers = [PackageRepositoryInfo])),
            default = {},
        ),
        "package_system": attrs.dep(providers = [PackageSystemInfo]),
        "required_repositories": attrs.list(attrs.dep(providers = [PackageRepositoryInfo])),
    },
)

def repository_universe(name: str, **kwargs) -> None:
    if not name.endswith(".repositories"):
        fail("repository_universe name must end with '.repositories': {}".format(name))
    _repository_universe(name = name, **kwargs)

def remote_repository_base(ctx: AnalysisContext, repo_dir: Artifact) -> list[Provider]:
    """Register the package-system-neutral interface to a remote repository."""
    rid = ctx.label.name
    system = ctx.attrs.package_system[PackageSystemInfo]
    spec = ctx.actions.write_json(
        "snapshot.spec.json",
        {"baseurl": ctx.attrs.baseurl, "id": rid},
        has_content_based_path = False,
    )
    sub_targets = {
        "manifest": [DefaultInfo(default_output = spec)],
        "snapshot": [DefaultInfo(), RunInfo(args = cmd_args(system.snapshot[RunInfo], "--spec", spec))],
    }
    return [
        DefaultInfo(default_output = repo_dir, sub_targets = sub_targets),
        PackageRepositoryInfo(
            baseurl = ctx.attrs.baseurl,
            dir = repo_dir,
            package_system = ctx.attrs.package_system,
        ),
    ]

def _contains_only(value: str, alphabet: str) -> bool:
    for character in value.elems():
        if character not in alphabet:
            return False
    return True

def _is_ascii(value: str) -> bool:
    for character in value.elems():
        if ord(character) > 127:
            return False
    return True

def _closure_name(canonical_name: str, checksum: str, suffix: str) -> str:
    # Retain a readable prefix while the full digest prevents NAME_MAX collisions.
    if not _is_ascii(canonical_name) or not _is_ascii(suffix):
        fail("package representation names must be ASCII: {!r}, {!r}".format(canonical_name, suffix))
    tail = "--" + checksum + suffix
    prefix_length = 255 - len(tail)
    if prefix_length <= 0:
        fail("package representation suffix is too long: {!r}".format(suffix))
    return canonical_name[:prefix_length] + tail

def _select_package_artifacts_impl(
    actions: AnalysisActions,
    tx: ArtifactValue,
    output: OutputArtifact,
    local_packages: dict[str, list[Artifact]],
    pools: dict[str, ResolvedDynamicValue],
    representation: str,
    suffix: str,
) -> list[Provider]:
    # Select already-owned artifacts; the transaction never creates new downloads.
    entries = tx.read_json()
    if type(entries) != type([]):
        fail("transaction is not a list (a frozen transaction still seeded `{}`?); resolve or remove it")

    by_repo = {rid: pool.providers[PackagePoolValueInfo].packages for rid, pool in pools.items()}
    artifacts = {}
    for entry in entries:
        if type(entry) != type({}):
            fail("transaction entry is not an object: {}".format(entry))
        source = entry.get("source")
        if source not in ("local", "repo"):
            fail("transaction entry has unknown source {!r}".format(source))
        required = ("package_id", "pkg_checksum", "repo", "source")
        missing = [key for key in required if key not in entry]
        if missing:
            fail("transaction entry lacks {}: {}".format(missing, entry))
        allowed = required + (("location",) if source == "local" else ("size", "url"))
        unknown = [key for key in entry.keys() if key not in allowed]
        if unknown:
            fail("transaction entry has unknown fields {}: {}".format(unknown, entry))

        rid = entry["repo"]
        checksum = entry["pkg_checksum"]
        package_id = entry["package_id"]
        if type(rid) != type("") or not rid or type(package_id) != type("") or not package_id:
            fail("transaction entry has invalid repo/package_id: {}".format(entry))
        if type(checksum) != type("") or len(checksum) != 64 or checksum != checksum.lower() or not _contains_only(checksum, "0123456789abcdef"):
            fail("transaction entry has invalid pkg_checksum: {}".format(entry))
        if source == "local":
            if representation != "installable":
                fail("local packages do not expose the {!r} representation".format(representation))

            package_dirs = local_packages.get(rid)
            if package_dirs == None:
                fail("local transaction entry names non-local repository '{}': {}".format(rid, entry))

            # Local hrefs identify an input directory and filename.
            location = entry.get("location")
            if type(location) != type(""):
                fail("local transaction entry lacks a location: {}".format(entry))
            parts = location.split("/")
            if len(parts) != 2 or not parts[0] or not _contains_only(parts[0], "0123456789") or not parts[1].endswith(suffix):
                fail("local transaction entry has invalid location: {}".format(entry))
            idx = int(parts[0])
            if idx >= len(package_dirs):
                fail("local transaction entry refers to missing package directory: {}".format(entry))
            output_name = _closure_name(parts[1], checksum, suffix)
            if output_name not in artifacts:
                artifacts[output_name] = package_dirs[idx].project(parts[1])
            continue

        url = entry.get("url")
        size = entry.get("size")
        if type(url) != type("") or not url:
            fail("remote transaction entry has invalid url: {}".format(entry))
        if type(size) != type(0) or size <= 0:
            fail("remote transaction entry has invalid size: {}".format(entry))

        if rid not in by_repo or checksum not in by_repo[rid]:
            fail(
                ("{} ({}/{}) is absent from the pinned repository package pool; " + "run refresh-catalog").format(package_id, rid, checksum),
            )
        package = by_repo[rid][checksum]
        if representation == "installable":
            artifact = package.artifact
            extension = suffix
        else:
            derived = package.representations.get(representation)
            if derived == None:
                fail("{} ({}/{}) has no {!r} representation".format(package_id, rid, checksum, representation))
            artifact = derived.artifact
            extension = derived.suffix
        output_name = _closure_name(package.name, checksum, extension)
        if output_name not in artifacts:
            artifacts[output_name] = artifact

    actions.symlinked_dir(output, artifacts)
    return []

_select_package_artifacts_action = dynamic_actions(
    impl = _select_package_artifacts_impl,
    attrs = {
        "local_packages": dynattrs.value(dict[str, list[Artifact]]),
        "output": dynattrs.output(),
        "pools": dynattrs.dict(str, dynattrs.dynamic_value()),
        "representation": dynattrs.value(str),
        "suffix": dynattrs.value(str),
        "tx": dynattrs.artifact_value(),
    },
)

def select_package_artifacts(
    ctx: AnalysisContext,
    tx: Artifact,
    repositories: list[Dependency],
    suffix: str,
    extra_packages: list[Artifact] = [],
    name: str = "install.closure",
    representation: str = "installable",
) -> Artifact:
    """Select one representation of each transaction package into a directory."""
    output = ctx.actions.declare_output(name, dir = True)
    pools = {repository.label.name: repository[PackagePoolInfo].value for repository in repositories if repository.get(PackagePoolInfo) != None}
    local_packages = {
        repository.label.name: repository[LocalPackageRepositoryInfo].package_dirs
        for repository in repositories
        if repository.get(LocalPackageRepositoryInfo) != None
    }
    if extra_packages:
        local_packages["extra"] = extra_packages
    ctx.actions.dynamic_output_new(
        _select_package_artifacts_action(
            tx = tx,
            output = output.as_output(),
            local_packages = local_packages,
            pools = pools,
            representation = representation,
            suffix = suffix,
        )
    )
    return output
