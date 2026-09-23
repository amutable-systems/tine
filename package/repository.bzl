# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Native package repositories, universes, and dynamic transaction selection."""

load(":system.bzl", "PackageSystemInfo")
load(":verify.bzl", "Verifier", "verify_packages")

PackageArtifactInfo = record(
    artifact = Artifact,
    name = str,
)

PackagePoolInfo = provider(
    doc = """A repository-owned dynamic pool of its packages, unverified.

    Only the code configuring a repository for a consumer reads it; the consumer selects from
    `ConfiguredPackageRepositoryInfo.packages`.""",
    fields = {"value": provider_field(DynamicValue)},
)

PackagePoolValueInfo = provider(
    doc = "A resolved authoritative package pool keyed by stable package id.",
    fields = {"packages": provider_field(dict[str, PackageArtifactInfo])},
)

def snapshot_data(snapshot: ArtifactValue, id: str) -> dict:
    """Read one repository's committed snapshot."""
    data = snapshot.read_json()
    if type(data) != type({}):
        fail("repository '{}' snapshot is not an object; run refresh-catalog".format(id))
    return data

def _retained_transports(id: str, box_locks: list[ArtifactValue]) -> dict[str, dict]:
    """The remote transports committed box locks still need from this repository."""
    retained = {}
    for lock in box_locks:
        for entry in lock.read_json():
            if entry["source"] != "repo" or entry["repo"] != id:
                continue

            checksum = entry["pkg_checksum"]
            package = {"size": entry["size"], "url": entry["url"]}
            previous = retained.get(checksum)
            if previous != None and previous["size"] != package["size"]:
                fail("repository '{}': retained package {} has conflicting sizes".format(id, checksum))

            # More than one box may retain the same content through different mirrors.
            if previous == None or package["url"] < previous["url"]:
                retained[checksum] = package
    return retained

def pool_transports(id: str, baseurl: str, snapshot: ArtifactValue, box_locks: list[ArtifactValue]) -> dict[str, dict]:
    """Where every package this repository owns can be fetched from, keyed by checksum.

    The pool is the union of what the repository advertises now and what committed box locks
    still need from it. The repository's current route wins while it still carries the content,
    so an advancing snapshot re-routes a package rather than duplicating it, and a lock's
    transport remains the fallback once the package leaves the snapshot. A size that disagrees
    between the two is skew this cannot paper over.
    """
    packages = _retained_transports(id, box_locks)
    for checksum, package in snapshot_data(snapshot, id).get("packages", {}).items():
        retained = packages.get(checksum)
        if retained != None and retained["size"] != package["size"]:
            fail("repository '{}': package {} has conflicting snapshot and lock sizes".format(id, checksum))
        packages[checksum] = {
            "size": package["size"],
            "url": baseurl.rstrip("/") + "/" + package["location"],
        }
    return packages

def _retained_metadata(id: str, box_locks: list[ArtifactValue], current: list, packages: dict) -> list[dict]:
    """The metadata generations committed box locks still need from this repository.

    A lock records the metadata that vouched for its packages. A generation is materialized only
    while one of those packages is absent from the current snapshot: until then the current
    metadata vouches for all of them, and afterwards the retained transport alone would fetch a
    package nothing can verify.
    """
    generations = {}
    for lock in box_locks:
        entries = lock.read_json()
        missing = [entry for entry in entries if entry["source"] == "repo" and entry["repo"] == id and entry["pkg_checksum"] not in packages]
        if not missing:
            continue
        for entry in entries:
            if entry["source"] != "metadata" or entry["repo"] != id:
                continue
            files = entry["files"]
            if type(files) != type([]) or files == current:
                continue
            generations[json.encode(files)] = entry
    return [generations[key] for key in sorted(generations)]

def _download(actions: AnalysisActions, path: str, file: dict) -> Artifact:
    out = actions.declare_output(path)
    actions.download_file(out, file["url"], sha256 = file["sha256"], size_bytes = int(file["size"]))
    return out

def _materialize_metadata_impl(
    actions: AnalysisActions,
    id: str,
    repo: OutputArtifact,
    snapshot: ArtifactValue,
    box_locks: list[ArtifactValue],
    pinned_at: str | None,
    vouches: bool,
) -> list[Provider]:
    data = snapshot_data(snapshot, id)
    metadata = data.get("metadata")
    if not metadata:
        fail("repository '{}' is not locked yet (snapshot missing or empty); run refresh-catalog".format(id))

    tree = {path: actions.write(path, content) for path, content in metadata["inline"].items()}
    for file in metadata["files"]:
        tree[file["out"]] = _download(actions, file["out"], file)
    if vouches:
        # A resolve records the manifest in the lock it writes, so what vouched for a package
        # is retained with its transport, and a lock's packages stay verifiable after the pin moves.
        manifest = {"files": metadata["files"], "pinned_at": pinned_at}
        tree[METADATA_MANIFEST] = actions.write_json(METADATA_MANIFEST, manifest)
        for index, generation in enumerate(_retained_metadata(id, box_locks, metadata["files"], data.get("packages", {}))):
            directory = "{}/{}".format(RETAINED_DIRECTORY, index)
            for file in generation["files"]:
                tree[directory + "/" + file["out"]] = _download(actions, directory + "/" + file["out"], file)
            # A generation is judged as of its own pin, the way the current one is as of the repository's.
            if generation.get("pinned_at") != None:
                tree[directory + "/" + PINNED_AT_FILE] = actions.write(directory + "/" + PINNED_AT_FILE, generation["pinned_at"])
    actions.copied_dir(repo, tree)
    return []

_materialize_metadata = dynamic_actions(
    impl = _materialize_metadata_impl,
    attrs = {
        "box_locks": dynattrs.list(dynattrs.artifact_value()),
        "id": dynattrs.value(str),
        "pinned_at": dynattrs.value(str | None),
        "repo": dynattrs.output(),
        "snapshot": dynattrs.artifact_value(),
        "vouches": dynattrs.value(bool),
    },
)

def _materialize_package_pool_impl(
    actions: AnalysisActions,
    baseurl: str,
    box_locks: list[ArtifactValue],
    id: str,
    snapshot: ArtifactValue,
    suffix: str,
) -> list[Provider]:
    pool = {}
    for checksum, package in pool_transports(id, baseurl, snapshot, box_locks).items():
        url = package["url"]
        artifact = actions.declare_output("packages", checksum + suffix, has_content_based_path = True)

        # A repository may serve a package larger than a Starlark i32, which arrives as a float.
        actions.download_file(artifact, url, sha256 = checksum, size_bytes = int(package["size"]))
        pool[checksum] = PackageArtifactInfo(artifact = artifact, name = url.rsplit("/", 1)[-1])
    return [PackagePoolValueInfo(packages = pool)]

_materialize_package_pool = dynamic_actions(
    impl = _materialize_package_pool_impl,
    attrs = {
        "baseurl": dynattrs.value(str),
        "box_locks": dynattrs.list(dynattrs.artifact_value()),
        "id": dynattrs.value(str),
        "snapshot": dynattrs.artifact_value(),
        "suffix": dynattrs.value(str),
    },
)

def _signing_keys(ctx: AnalysisContext) -> dict[str, Artifact | None]:
    """Map each declared signing key fingerprint to its committed file, or None until refresh fetches it."""
    rid = ctx.label.name
    if ctx.attrs.package_system[PackageSystemInfo].verify != None and not ctx.attrs.signing_keys:
        fail("repository '{}' declares no signing_keys, and its package system verifies signatures".format(rid))
    for fingerprint in ctx.attrs.signing_keys:
        if len(fingerprint) != 40 or not _contains_only(fingerprint, "0123456789ABCDEF"):
            fail("repository '{}': signing key {!r} is not an upper-case 40 hex digit fingerprint".format(rid, fingerprint))
    # The files are globbed by declared fingerprint, so each one's name is a declared key.
    files = {file.basename.removesuffix(SIGNING_KEY_SUFFIX): file for file in ctx.attrs.signing_key_files}
    return {fingerprint: files.get(fingerprint) for fingerprint in ctx.attrs.signing_keys}

def _remote_repository_impl(ctx: AnalysisContext) -> list[Provider]:
    repo = ctx.actions.declare_output("repo", dir = True)
    snapshot = ctx.attrs.snapshot
    if snapshot == None:
        snapshot = ctx.actions.write("empty-snapshot.json", "{}")
    ctx.actions.dynamic_output_new(
        _materialize_metadata(
            box_locks = ctx.attrs.box_locks,
            id = ctx.label.name,
            pinned_at = ctx.attrs.pinned_at,
            repo = repo.as_output(),
            snapshot = snapshot,
            vouches = ctx.attrs.package_system[PackageSystemInfo].metadata_vouches,
        )
    )
    return remote_repository_base(
        ctx,
        baseurl = ctx.attrs.baseurl,
        package_system = ctx.attrs.package_system,
        pinned_at = ctx.attrs.pinned_at,
        repo_dir = repo,
        signing_keys = _signing_keys(ctx),
        snapshot_spec = ctx.attrs.snapshot_spec,
    ) + [
        PackagePoolInfo(
            value = _declare_package_pool(
                ctx,
                baseurl = ctx.attrs.baseurl,
                box_locks = ctx.attrs.box_locks,
                package_system = ctx.attrs.package_system,
                snapshot = snapshot,
            ),
        ),
    ]

def _declare_package_pool(
    ctx: AnalysisContext,
    *,
    baseurl: str,
    box_locks: list[Artifact],
    package_system: Dependency,
    snapshot: Artifact,
) -> DynamicValue:
    """Declare the authoritative pool of packages this repository owns.

    Every package is fetched by the checksum that identifies it, so a pool entry is named after
    that checksum and the suffix its package system names a selected package with. What a
    repository serves is its own business; what it is called is not.
    """
    return ctx.actions.dynamic_output_new(
        _materialize_package_pool(
            baseurl = baseurl,
            box_locks = box_locks,
            id = ctx.label.name,
            snapshot = snapshot,
            suffix = package_system[PackageSystemInfo].package_suffix,
        )
    )

PackageRepositoryInfo = provider(
    doc = "A repository belonging to one native package system.",
    fields = {
        "baseurl": provider_field(str | None, default = None),
        # Local declarations are materialized by a consuming package manager.
        "dir": provider_field(Artifact | None, default = None),
        "package_system": provider_field(Dependency),
        # When the pinned snapshot was published, ISO 8601 in UTC; None for a rolling repository.
        "pinned_at": provider_field(str | None, default = None),
        # The fingerprints of the keys one of which must have signed each package, each with its
        # committed key file, or None until refresh-catalog has fetched it. Empty for a local
        # repository: what is built here is unsigned and vouched for by Buck.
        "signing_keys": provider_field(dict[str, Artifact | None], default = {}),
    },
)

# Where a catalog keeps a fetched signing key, named by its fingerprint like rpm names an imported one.
SIGNING_KEY_DIRECTORY = "snapshot/key"
SIGNING_KEY_SUFFIX = ".key"

# What a materialized repository whose metadata vouches for its packages carries beyond the snapshot's
# files: the manifest of those files, and the generations committed locks retain, one subdirectory
# each with the time it was pinned at. Keep in sync with MANIFEST, RETAINED and PINNED_AT in
# snapshotter.py.
METADATA_MANIFEST = "metadata.json"
RETAINED_DIRECTORY = "retained"
PINNED_AT_FILE = "pinned_at"

ConfiguredPackageRepositoryInfo = record(
    id = str,
    dependency = field(Dependency | None, default = None),
    directory = Artifact,
    priority = int,
    baseurl = field(str | None, default = None),
    # The packages a consumer may select, a dynamic value resolving to a PackagePoolValueInfo.
    # None for a local repository, whose packages are its input directories.
    packages = field(DynamicValue | None, default = None),
    # The `repository_verifier` for a selected closure.
    verifier = field(Verifier | None, default = None),
)

# What every remote repository declares, whichever package system owns it.
_REMOTE_REPOSITORY_ATTRS = {
    "baseurl": attrs.string(),
    "box_locks": attrs.list(
        attrs.source(),
        default = [],
        doc = "frozen box transactions whose remote package transports remain available",
    ),
    "labels": attrs.list(attrs.string(), default = []),
    "package_system": attrs.dep(providers = [PackageSystemInfo]),
    "pinned_at": attrs.option(
        attrs.string(),
        default = None,
        doc = "when the pinned snapshot was published, ISO 8601 in UTC; a verifier judges key expiry as of then",
    ),
    "signing_key_files": attrs.list(
        attrs.source(),
        default = [],
        doc = "the declared signing keys refresh-catalog has fetched so far, one file per fingerprint",
    ),
    "signing_keys": attrs.dict(
        attrs.string(),
        attrs.string(),
        default = {},
        doc = "fingerprint of each key that may sign this repository's packages, and where refresh-catalog fetches it",
    ),
    "snapshot": attrs.option(attrs.source(), default = None),
    "snapshot_spec": attrs.dict(
        attrs.string(),
        attrs.string(),
        default = {},
        doc = "what else this repository's snapshot driver needs to name its metadata",
    ),
}

remote_repository = rule(impl = _remote_repository_impl, attrs = _REMOTE_REPOSITORY_ATTRS)

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

def remote_repository_base(
    ctx: AnalysisContext,
    *,
    baseurl: str,
    package_system: Dependency,
    repo_dir: Artifact,
    signing_keys: dict[str, Artifact | None],
    snapshot_spec: dict[str, typing.Any] = {},
    pinned_at: str | None = None,
) -> list[Provider]:
    """Register the package-system-neutral interface to a remote repository.

    `snapshot_spec` carries whatever else one package system's snapshot driver needs to name
    its metadata; the identity and base URL every repository has are supplied here.
    """
    rid = ctx.label.name
    system = package_system[PackageSystemInfo]
    reserved = [key for key in snapshot_spec if key in ("baseurl", "id")]
    if reserved:
        fail("remote_repository_base: {} are supplied by the neutral spec".format(reserved))
    spec = ctx.actions.write_json(
        "snapshot.spec.json",
        dict(snapshot_spec, baseurl = baseurl, id = rid),
        has_content_based_path = False,
    )
    sub_targets = {
        "manifest": [DefaultInfo(default_output = spec)],
        "snapshot": [DefaultInfo(), RunInfo(args = cmd_args(system.snapshot[RunInfo], "--spec", spec))],
    }
    return [
        DefaultInfo(default_output = repo_dir, sub_targets = sub_targets),
        PackageRepositoryInfo(
            baseurl = baseurl,
            dir = repo_dir,
            package_system = package_system,
            pinned_at = pinned_at,
            signing_keys = signing_keys,
        ),
    ]

RepositoryPin = record(
    # Where the pinned snapshot serves this repository from.
    baseurl = field(str),
    # What refresh-catalog reads back to advance the pin, under the namespace its system owns.
    metadata = field(dict[str, str]),
    # When the pinned snapshot was published, ISO 8601 in UTC, for a verifier to judge key expiry
    # as of then rather than as of the build: a pin freezes the trust data, so the clock goes with it.
    pinned_at = field(str | None, default = None),
)

def declare_remote_repository(
    *,
    name: str,
    what: str,
    label: str,
    package_system: str,
    baseurl: str | None,
    pin: RepositoryPin | None,
    labels: list[str] = [],
    signing_keys: dict[str, str] = {},
    **kwargs,
) -> None:
    """Declare a remote repository backed by its optional package-relative snapshot.

    A repository is named either by a plain `baseurl` or by a pin, which composes the base URL
    from a mirror publishing immutable snapshots and records what refresh-catalog advances. Only
    a mirror whose metadata never changes keeps a committed snapshot buildable, so the pin belongs
    on the declaration a catalog writes and releases forward their own pin arguments to it.

    `signing_keys` maps the approved key fingerprints to the corresponding key download URL.

    How a pin composes its URL is the package system's business; everything around that is not.
    """
    if not name.endswith(".repository"):
        fail("{} name must end with '.repository': {}".format(what, name))
    if pin != None and baseurl != None:
        fail("{} takes a pin or baseurl, not both: {}".format(what, name))
    if pin == None and baseurl == None:
        fail("{} requires baseurl or a pin: {}".format(what, name))
    snapshots = glob(["snapshot/repo/" + name.removesuffix(".repository") + ".json"])
    remote_repository(
        name = name,
        baseurl = pin.baseurl if pin != None else baseurl,
        box_locks = glob(["snapshot/box/*.json"]),
        pinned_at = pin.pinned_at if pin != None else None,
        labels = ["tine:remote-repository", label] + labels,
        metadata = pin.metadata if pin != None else {},
        package_system = package_system,
        signing_key_files = glob([SIGNING_KEY_DIRECTORY + "/" + fingerprint + SIGNING_KEY_SUFFIX for fingerprint in signing_keys]),
        signing_keys = signing_keys,
        snapshot = snapshots[0] if snapshots else None,
        **kwargs,
    )

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
    name: str,
    pools: dict[str, ResolvedDynamicValue],
    suffix: str,
    verifiers: dict[str, Verifier],
) -> list[Provider]:
    # Select already-owned artifacts; the transaction never creates new downloads.
    entries = tx.read_json()
    if type(entries) != type([]):
        fail("transaction is not a list (a frozen transaction still seeded `{}`?); resolve or remove it")

    by_repo = {rid: pool.providers[PackagePoolValueInfo].packages for rid, pool in pools.items()}
    artifacts = {}
    selected = {}
    for entry in entries:
        if type(entry) != type({}):
            fail("transaction entry is not an object: {}".format(entry))
        source = entry.get("source")
        if source == "metadata":
            # What vouched for the packages; the repository reads it when materializing its metadata.
            if type(entry.get("repo")) != type("") or type(entry.get("files")) != type([]) or type(entry.get("pinned_at")) != type(""):
                fail("transaction metadata entry lacks a repo, files or pinned_at: {}".format(entry))
            continue
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
            artifacts[_closure_name(parts[1], checksum, suffix)] = package_dirs[idx].project(parts[1])
            continue

        url = entry.get("url")
        size = entry.get("size")
        if type(url) != type("") or not url:
            fail("remote transaction entry has invalid url: {}".format(entry))
        # A repository may serve a package larger than a Starlark i32, which arrives as a float.
        if type(size) not in (type(0), type(0.0)) or size <= 0:
            fail("remote transaction entry has invalid size: {}".format(entry))

        if rid not in by_repo or checksum not in by_repo[rid]:
            fail(
                ("{} ({}/{}) is absent from the pinned repository package pool; " + "run refresh-catalog").format(package_id, rid, checksum),
            )
        package = by_repo[rid][checksum]
        selected.setdefault(rid, {})[_closure_name(package.name, checksum, suffix)] = package.artifact

    for rid, packages in selected.items():
        if rid in verifiers:
            verified = verify_packages(actions, name, rid, verifiers[rid], packages)
            packages = {output_name: verified.project(output_name) for output_name in packages}
        artifacts |= packages
    actions.symlinked_dir(output, artifacts)
    return []

_select_package_artifacts_action = dynamic_actions(
    impl = _select_package_artifacts_impl,
    attrs = {
        "local_packages": dynattrs.value(dict[str, list[Artifact]]),
        "name": dynattrs.value(str),
        "output": dynattrs.output(),
        "pools": dynattrs.dict(str, dynattrs.dynamic_value()),
        "suffix": dynattrs.value(str),
        "tx": dynattrs.artifact_value(),
        "verifiers": dynattrs.value(dict[str, Verifier]),
    },
)

def select_package_artifacts(
    ctx: AnalysisContext,
    tx: Artifact,
    repositories: list[ConfiguredPackageRepositoryInfo],
    suffix: str,
    extra_packages: list[Artifact] = [],
    name: str = "install.closure",
) -> Artifact:
    """Select each transaction package's artifact into a directory.

    For a repository configured with a verifier, the directory holds verified copies.
    """
    output = ctx.actions.declare_output(name, dir = True)
    pools = {}
    verifiers = {}
    local_packages = {}
    for configured in repositories:
        repository = configured.dependency
        if repository == None:
            # Internal consistency check: Only the planner's inline `extra` repository has None, and its
            # packages arrive as `extra_packages` rather than through the configured repo list.
            fail("select_package_artifacts: repository '{}' has no declaration".format(configured.id))
        if repository.get(LocalPackageRepositoryInfo) != None:
            local_packages[configured.id] = repository[LocalPackageRepositoryInfo].package_dirs
            continue
        if configured.packages == None:
            # Internal consistency check: a remote repository is configured with its packages.
            fail("select_package_artifacts: repository '{}' is configured without packages".format(configured.id))
        pools[configured.id] = configured.packages
        if configured.verifier != None:
            verifiers[configured.id] = configured.verifier
    if extra_packages:
        local_packages["extra"] = extra_packages
    ctx.actions.dynamic_output_new(
        _select_package_artifacts_action(
            tx = tx,
            output = output.as_output(),
            local_packages = local_packages,
            name = name,
            pools = pools,
            suffix = suffix,
            verifiers = verifiers,
        )
    )
    return output
