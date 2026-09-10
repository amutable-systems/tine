"""Verify a repository's packages as a closure selects them."""

load("//:specs.bzl", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load(":system.bzl", "PackageSystemInfo")

# What verifies one repository's packages for one consumer: the keyring of the repository's declared
# keys, and the package system's verify program run in the consumer's box.
Verifier = record(
    keyring = Artifact,
    verify = RunInfo,
)

def repository_verifier(
    ctx: AnalysisContext,
    box: BoxInfo,
    id: str,
    signing_keys: dict[str, Artifact | None],
    package_system: Dependency,
) -> Verifier | None:
    """The verifier for repository `id`'s packages in a consumer that verifies with `box`.

    The keyring is built from the declared keys, any others are refused. None for a repository without
    declared keys.
    """
    if not signing_keys:
        return None
    system = package_system[PackageSystemInfo]
    if system.verify == None or system.keyring == None:
        fail("repository '{}': package system {} verifies no signatures".format(id, package_system.label))
    missing = [fingerprint for fingerprint, file in signing_keys.items() if file == None]
    if missing:
        fail("repository '{}': signing key(s) {} are not in the catalog yet; run refresh-catalog".format(id, missing))

    keyring = ctx.actions.declare_output(id + ".keyring", dir = True)
    ctx.actions.run(
        cmd_args(
            box_run(box = box, exe = system.keyring),
            spec_args(ctx.actions, id + ".keyring.spec.json", {"keys": signing_keys, "out": keyring.as_output()}),
        ),
        category = "keyring",
        identifier = id,
    )
    return Verifier(keyring = keyring, verify = box_run(box = box, exe = system.verify))

def verify_packages(
    actions: AnalysisActions,
    name: str,
    id: str,
    verifier: Verifier,
    packages: dict[str, Artifact],
) -> Artifact:
    """Verify what closure `name` selects from repository `id`, publishing a directory of copies named by key."""
    out = actions.declare_output(name + "." + id + ".verified", dir = True)
    actions.run(
        cmd_args(
            verifier.verify,
            spec_args(
                actions,
                name + "." + id + ".verify.spec.json",
                {"keyring": verifier.keyring, "out": out.as_output(), "packages": packages},
            ),
        ),
        category = "verify",
        identifier = name + "/" + id,
    )
    return out
