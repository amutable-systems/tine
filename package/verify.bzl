# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Verify a repository's packages as a closure selects them."""

load("//:specs.bzl", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load(":system.bzl", "PackageSystemInfo")

# What verifies one repository's packages for one consumer: the keyring of the repository's declared
# keys, the package system's verify program run in the consumer's box, and the repository's pinned
# metadata, for a system whose signatures travel in the database rather than the package.
Verifier = record(
    keyring = Artifact,
    verify = RunInfo,
    repository = Artifact,
    # What else this package system's verify driver needs to name the metadata it follows.
    spec = field(dict[str, str], {}),
)

def repository_verifier(
    ctx: AnalysisContext,
    box: BoxInfo,
    id: str,
    signing_keys: dict[str, Artifact | None],
    package_system: Dependency,
    directory: Artifact,
    keyrings: dict[str, Artifact],
    pinned_at: str | None = None,
    verify_spec: dict[str, str] = {},
) -> Verifier | None:
    """The verifier for repository `id`'s packages in a consumer that verifies with `box`.

    The keyring is built from the declared keys, any others are refused. None for a repository without
    declared keys. Repositories declaring the same keys share one keyring: `keyrings` is the caller's
    cache of those built so far, one per consumer and package system, so the same key set is not
    imported and certified once per repository that trusts it.

    `pinned_at` is when the repository's snapshot was published: the keyring judges key expiry as of
    then, so a pinned snapshot verifies the same way however long after it is built.

    `verify_spec` is what the repository was declared as, for a driver that has to check the pinned
    metadata is the metadata that declaration asked for.
    """
    if not signing_keys:
        return None
    system = package_system[PackageSystemInfo]
    if system.verify == None or system.keyring == None:
        fail("repository '{}': package system {} verifies no signatures".format(id, package_system.label))
    missing = [fingerprint for fingerprint, file in signing_keys.items() if file == None]
    if missing:
        fail("repository '{}': signing key(s) {} are not in the catalog yet; run refresh-catalog".format(id, missing))

    fingerprints = sorted(signing_keys)
    key = str(package_system.label) + " " + str(pinned_at) + " " + " ".join(fingerprints)
    keyring = keyrings.get(key)
    if keyring == None:
        # Named by the keys' short ids: readable, and a collision between distinct sets declares the
        # same output twice, which fails loudly rather than sharing wrongly.
        name = "keyring-" + "-".join([fingerprint[-8:] for fingerprint in fingerprints])
        keyring = ctx.actions.declare_output(name, dir = True)
        ctx.actions.run(
            cmd_args(
                box_run(box = box, exe = system.keyring),
                spec_args(
                    ctx.actions,
                    name + ".spec.json",
                    {"keys": signing_keys, "out": keyring.as_output(), "time": pinned_at},
                ),
            ),
            category = "keyring",
            identifier = name,
        )
        keyrings[key] = keyring
    return Verifier(
        keyring = keyring,
        verify = box_run(box = box, exe = system.verify),
        repository = directory,
        spec = verify_spec,
    )

def verify_packages(
    actions: AnalysisActions,
    name: str,
    id: str,
    verifier: Verifier,
    packages: dict[str, Artifact],
) -> Artifact:
    """Verify what closure `name` selects from repository `id`, publishing a directory of copies named by key."""
    out = actions.declare_output(name + "." + id + ".verified", dir = True)
    neutral = {
        "keyring": verifier.keyring,
        "out": out.as_output(),
        "packages": packages,
        "repository": verifier.repository,
    }
    reserved = [key for key in verifier.spec if key in neutral]
    if reserved:
        fail("verify_packages: {} are supplied by the neutral spec".format(reserved))
    actions.run(
        cmd_args(
            verifier.verify,
            spec_args(actions, name + "." + id + ".verify.spec.json", neutral | verifier.spec),
        ),
        category = "verify",
        identifier = name + "/" + id,
    )
    return out
