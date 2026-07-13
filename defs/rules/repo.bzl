"""Repositories: pinned metadata, package artifacts, and dynamic transaction selection."""

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

RepoInfo = provider(
    doc = "A package repository.",
    fields = {
        "id": provider_field(str),
        "dir": provider_field(Artifact),
        # DNF uses lower numbers first; 99 is its default.
        "priority": provider_field(int, default = 99),
    },
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

def _closure_name(canonical_name: str, pkgid: str, suffix: str) -> str:
    # Retain a readable prefix while the full digest prevents NAME_MAX collisions.
    if not _is_ascii(canonical_name) or not _is_ascii(suffix):
        fail("package representation names must be ASCII: {!r}, {!r}".format(canonical_name, suffix))
    tail = "--" + pkgid + suffix
    prefix_length = 255 - len(tail)
    if prefix_length <= 0:
        fail("package representation suffix is too long: {!r}".format(suffix))
    return canonical_name[:prefix_length] + tail

def _download_closure(
        actions: AnalysisActions,
        tx: ArtifactValue,
        closure: OutputArtifact,
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
    rpms = {}
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
            if output_name not in rpms:
                rpms[output_name] = extra_packages[idx].project(parts[1])
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
        if output_name not in rpms:
            rpms[output_name] = artifact

    actions.symlinked_dir(closure, rpms)
    return []

_download = dynamic_actions(
    impl = _download_closure,
    attrs = {
        "tx": dynattrs.artifact_value(),
        "closure": dynattrs.output(),
        "extra_packages": dynattrs.value(list[Artifact]),
        "pools": dynattrs.dict(str, dynattrs.dynamic_value()),
        "representation": dynattrs.value(str),
    },
)

def download_closure(
        ctx: AnalysisContext,
        tx: Artifact,
        repositories: list[Dependency],
        extra_packages: list[Artifact] = [],
        name: str = "install.closure",
        representation: str = "installable") -> Artifact:
    closure = ctx.actions.declare_output(name, dir = True)
    pools = {
        repository[RepoInfo].id: repository[PackagePoolInfo].value
        for repository in repositories
        if repository.get(PackagePoolInfo) != None
    }
    ctx.actions.dynamic_output_new(_download(
        tx = tx,
        closure = closure.as_output(),
        extra_packages = extra_packages,
        pools = pools,
        representation = representation,
    ))
    return closure
