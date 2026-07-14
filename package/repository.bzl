"""Native package repositories, universes, and dynamic transaction selection."""

load(":system.bzl", "PackageSystemInfo")

package_representation = record(
    artifact = Artifact,
    suffix = str,
)

package_artifact = record(
    artifact = Artifact,
    metadata = typing.Any,
    name = str,
    representations = dict[str, package_representation],
)

PackagePoolInfo = provider(
    doc = "A repository-owned dynamic package pool.",
    fields = {"value": provider_field(DynamicValue)},
)

PackagePoolValueInfo = provider(
    doc = "A resolved authoritative package pool keyed by stable package id.",
    # The same rich records also back format-specific providers without copying the map.
    fields = {"packages": provider_field(dict[str, package_artifact])},
)

PackageRepositoryInfo = provider(
    doc = "A repository belonging to one native package system.",
    fields = {
        "id": provider_field(str),
        "dir": provider_field(Artifact),
        "package_system": provider_field(Dependency),
        # DNF uses lower numbers first; 99 is its default.
        "priority": provider_field(int, default = 99),
    },
)

RepositoryUniverseInfo = provider(
    doc = "A homogeneous repository universe and its default selection policy.",
    fields = {
        "package_system": provider_field(Dependency),
        "required_repositories": provider_field(list[Dependency]),
        "optional_repository_groups": provider_field(dict[str, list[Dependency]]),
        "default_repository_groups": provider_field(list[str]),
    },
)

def _add_repository(
        package_system: Dependency,
        repository: Dependency,
        repositories: list[Dependency],
        by_id: dict[str, Dependency]) -> None:
    repo = repository[PackageRepositoryInfo]
    if repo.package_system.label != package_system.label:
        fail(
            "repository '{}' uses package system {}, expected {}".format(
                repo.id,
                repo.package_system.label,
                package_system.label,
            ),
        )
    previous = by_id.get(repo.id)
    if previous != None:
        if previous.label != repository.label:
            fail("repository id '{}' is provided by both {} and {}".format(repo.id, previous.label, repository.label))
        return
    by_id[repo.id] = repository
    repositories.append(repository)

def merge_repositories(
        package_system: Dependency,
        candidates: list[Dependency]) -> list[Dependency]:
    """Validate and de-duplicate repositories while preserving declaration order."""
    repositories = []
    by_id = {}

    for repository in candidates:
        _add_repository(package_system, repository, repositories, by_id)

    return repositories

def select_repositories(
        universe: Dependency,
        enable_repository_groups: list[str],
        disable_repository_groups: list[str]) -> list[Dependency]:
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
        "package_system": attrs.dep(providers = [PackageSystemInfo]),
        "required_repositories": attrs.list(attrs.dep(providers = [PackageRepositoryInfo])),
        "optional_repository_groups": attrs.dict(
            attrs.string(),
            attrs.list(attrs.dep(providers = [PackageRepositoryInfo])),
            default = {},
        ),
        "default_repository_groups": attrs.list(attrs.string(), default = []),
    },
)

def repository_universe(name: str, **kwargs) -> None:
    if not name.endswith(".repositories"):
        fail("repository_universe name must end with '.repositories': {}".format(name))
    _repository_universe(
        name = name,
        **kwargs
    )

def remote_repository_base(ctx: AnalysisContext, repo_dir: Artifact) -> list[Provider]:
    """Register the package-system-neutral interface to a remote repository."""
    rid = ctx.label.name
    system = ctx.attrs.package_system[PackageSystemInfo]
    manifest = ctx.actions.write("manifest.json", json.encode({"baseurl": ctx.attrs.baseurl, "id": rid}))
    sub_targets = {
        "manifest": [DefaultInfo(default_output = manifest)],
        "snapshot": [DefaultInfo(), RunInfo(args = cmd_args(system.snapshot[RunInfo], "--manifest", manifest))],
    }
    return [
        DefaultInfo(default_output = repo_dir, sub_targets = sub_targets),
        PackageRepositoryInfo(
            id = rid,
            dir = repo_dir,
            package_system = ctx.attrs.package_system,
            priority = ctx.attrs.priority,
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

def _closure_name(canonical_name: str, pkgid: str, suffix: str) -> str:
    # Retain a readable prefix while the full digest prevents NAME_MAX collisions.
    if not _is_ascii(canonical_name) or not _is_ascii(suffix):
        fail("package representation names must be ASCII: {!r}, {!r}".format(canonical_name, suffix))
    tail = "--" + pkgid + suffix
    prefix_length = 255 - len(tail)
    if prefix_length <= 0:
        fail("package representation suffix is too long: {!r}".format(suffix))
    return canonical_name[:prefix_length] + tail

def _select_package_artifacts_impl(
        actions: AnalysisActions,
        tx: ArtifactValue,
        output: OutputArtifact,
        extra_packages: list[Artifact],
        pools: dict[str, ResolvedDynamicValue],
        representation: str) -> list[Provider]:
    # Select already-owned artifacts; the transaction never creates new downloads.
    entries = tx.read_json()
    if type(entries) != type([]):
        fail("transaction is not a list (an engine lock still seeded `{}`?); run refresh-catalog")

    by_repo = {
        rid: pool.providers[PackagePoolValueInfo].packages
        for rid, pool in pools.items()
    }
    artifacts = {}
    for entry in entries:
        if type(entry) != type({}):
            fail("transaction entry is not an object: {}".format(entry))
        source = entry.get("source")
        if source not in ("local", "repo"):
            fail("transaction entry has unknown source {!r}".format(source))
        required = ("nevra", "pkgid", "repo", "source")
        missing = [key for key in required if key not in entry]
        if missing:
            fail("transaction entry lacks {}: {}".format(missing, entry))
        allowed = required + (("location",) if source == "local" else ())
        unknown = [key for key in entry.keys() if key not in allowed]
        if unknown:
            fail("transaction entry has unknown fields {}: {}".format(unknown, entry))

        rid = entry["repo"]
        pkgid = entry["pkgid"]
        nevra = entry["nevra"]
        if type(rid) != type("") or not rid or type(nevra) != type("") or not nevra:
            fail("transaction entry has invalid repo/nevra: {}".format(entry))
        if (
            type(pkgid) != type("") or
            len(pkgid) != 64 or
            pkgid != pkgid.lower() or
            not _contains_only(pkgid, "0123456789abcdef")
        ):
            fail("transaction entry has invalid pkgid: {}".format(entry))
        if source == "local":
            if representation != "installable":
                fail("local packages do not expose the {!r} representation".format(representation))

            # Local hrefs identify an input directory and filename.
            location = entry.get("location")
            if type(location) != type(""):
                fail("local transaction entry lacks a location: {}".format(entry))
            parts = location.split("/")
            if (
                len(parts) != 2 or
                not parts[0] or
                not _contains_only(parts[0], "0123456789") or
                not parts[1].endswith(".rpm")
            ):
                fail("local transaction entry has invalid location: {}".format(entry))
            idx = int(parts[0])
            if idx >= len(extra_packages):
                fail("local transaction entry refers to missing package directory: {}".format(entry))
            output_name = _closure_name(parts[1], pkgid, ".rpm")
            if output_name not in artifacts:
                artifacts[output_name] = extra_packages[idx].project(parts[1])
            continue

        if rid not in by_repo or pkgid not in by_repo[rid]:
            fail(
                ("{} ({}/{}) is absent from the pinned repository package pool; " +
                 "run refresh-catalog").format(nevra, rid, pkgid),
            )
        package = by_repo[rid][pkgid]
        if representation == "installable":
            artifact = package.artifact
            extension = ".rpm"
        else:
            derived = package.representations.get(representation)
            if derived == None:
                fail("{} ({}/{}) has no {!r} representation".format(nevra, rid, pkgid, representation))
            artifact = derived.artifact
            extension = derived.suffix
        output_name = _closure_name(package.name, pkgid, extension)
        if output_name not in artifacts:
            artifacts[output_name] = artifact

    actions.symlinked_dir(output, artifacts)
    return []

_select_package_artifacts_action = dynamic_actions(
    impl = _select_package_artifacts_impl,
    attrs = {
        "tx": dynattrs.artifact_value(),
        "output": dynattrs.output(),
        "extra_packages": dynattrs.value(list[Artifact]),
        "pools": dynattrs.dict(str, dynattrs.dynamic_value()),
        "representation": dynattrs.value(str),
    },
)

def select_package_artifacts(
        ctx: AnalysisContext,
        tx: Artifact,
        repositories: list[Dependency],
        extra_packages: list[Artifact] = [],
        name: str = "install.closure",
        representation: str = "installable") -> Artifact:
    """Select one representation of each transaction package into a directory."""
    output = ctx.actions.declare_output(name, dir = True)
    pools = {
        repository[PackageRepositoryInfo].id: repository[PackagePoolInfo].value
        for repository in repositories
        if repository.get(PackagePoolInfo) != None
    }
    ctx.actions.dynamic_output_new(_select_package_artifacts_action(
        tx = tx,
        output = output.as_output(),
        extra_packages = extra_packages,
        pools = pools,
        representation = representation,
    ))
    return output
