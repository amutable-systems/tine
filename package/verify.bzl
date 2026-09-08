"""Verify a repository's packages as they are selected."""

load("//:specs.bzl", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load(
    ":repository.bzl",
    "PackageArtifactInfo",
    "PackagePoolInfo",
    "PackagePoolValueInfo",
    "PackageRepositoryInfo",
)
load(":system.bzl", "PackageSystemInfo")

def _verify_packages_impl(
    actions: AnalysisActions,
    id: str,
    keyring: Artifact,
    pool: ResolvedDynamicValue,
    suffix: str,
    verify: RunInfo,
) -> list[Provider]:
    # One action per package, on demand like the downloads behind them. Named by repository as well
    # as checksum: one consumer verifies several repositories, which may serve the same file.
    verified = {}
    for checksum, package in pool.providers[PackagePoolValueInfo].packages.items():
        out = actions.declare_output(id + ".verified", checksum + suffix, has_content_based_path = True)
        actions.run(
            cmd_args(
                verify,
                spec_args(
                    actions,
                    id + ".verify/" + checksum + ".spec.json",
                    {"keyring": keyring, "name": package.name, "out": out.as_output(), "package": package.artifact},
                ),
            ),
            category = "verify",
            identifier = id + "/" + checksum,
        )
        verified[checksum] = PackageArtifactInfo(artifact = out, name = package.name)
    return [PackagePoolValueInfo(packages = verified)]

_verify_packages = dynamic_actions(
    impl = _verify_packages_impl,
    attrs = {
        "id": dynattrs.value(str),
        "keyring": dynattrs.value(Artifact),
        "pool": dynattrs.dynamic_value(),
        "suffix": dynattrs.value(str),
        "verify": dynattrs.value(RunInfo),
    },
)

def repository_packages(ctx: AnalysisContext, box: BoxInfo, repository: Dependency) -> DynamicValue:
    """A remote repository's packages for a consumer that verifies with `box`.

    Each is verified on demand against the repository's declared signing keys; the packages of a
    repository without keys stay unverified. The keyring is built once per repository from its
    declared keys, any others are refused.
    """
    rid = repository.label.name
    repo = repository[PackageRepositoryInfo]
    pool = repository[PackagePoolInfo].value
    if not repo.signing_keys:
        return pool
    system = repo.package_system[PackageSystemInfo]
    if system.verify == None or system.keyring == None:
        fail("repository '{}': package system {} verifies no signatures".format(rid, repo.package_system.label))
    missing = [fingerprint for fingerprint, file in repo.signing_keys.items() if file == None]
    if missing:
        fail("repository '{}': signing key(s) {} are not in the catalog yet; run refresh-catalog".format(rid, missing))

    keyring = ctx.actions.declare_output(rid + ".keyring", dir = True)
    ctx.actions.run(
        cmd_args(
            box_run(box = box, exe = system.keyring),
            spec_args(ctx.actions, rid + ".keyring.spec.json", {"keys": repo.signing_keys, "out": keyring.as_output()}),
        ),
        category = "keyring",
        identifier = rid,
    )
    return ctx.actions.dynamic_output_new(
        _verify_packages(
            id = rid,
            keyring = keyring,
            pool = pool,
            suffix = system.package_suffix,
            verify = box_run(box = box, exe = system.verify),
        )
    )
