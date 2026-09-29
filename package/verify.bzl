# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Verify a repository's packages as a closure selects them."""

load("//:specs.bzl", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load(":system.bzl", "PackageSystemInfo")

# What verifies one repository's packages for one consumer: the declared keys, the package system's
# verify program run in the consumer's box, and the repository's pinned metadata, for a system whose
# signatures travel in the database rather than the package.
Verifier = record(
    # What the verify spec names the keys by: the `keyring` built from them, or for a system that
    # builds none, the declared `keys` as they are and the `time` to judge them as of.
    keys = dict[str, typing.Any],
    verify = RunInfo,
    repository = Artifact,
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
) -> Verifier | None:
    """The verifier for repository `id`'s packages in a consumer that verifies with `box`.

    The keyring is built from the declared keys, any others are refused. None for a repository without
    declared keys. Repositories declaring the same keys share one keyring: `keyrings` is the caller's
    cache of those built so far, one per consumer and package system, so the same key set is not
    imported and certified once per repository that trusts it. A system whose verify program reads
    key files as they are has no keyring driver, and is handed the declared ones.

    `pinned_at` is when the repository's snapshot was published: signatures are judged as of then,
    so a pinned snapshot verifies the same way however long after it is built.
    """
    if not signing_keys:
        return None
    system = package_system[PackageSystemInfo]
    if system.verify == None:
        fail("repository '{}': package system {} verifies no signatures".format(id, package_system.label))
    missing = [fingerprint for fingerprint, file in signing_keys.items() if file == None]
    if missing:
        fail("repository '{}': signing key(s) {} are not in the catalog yet; run refresh-catalog".format(id, missing))
    verify = box_run(box = box, exe = system.verify)
    if system.keyring == None:
        return Verifier(keys = {"keys": signing_keys, "time": pinned_at}, verify = verify, repository = directory)

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
    return Verifier(keys = {"keyring": keyring}, verify = verify, repository = directory)

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
                {
                    "out": out.as_output(),
                    "packages": packages,
                    "repository": verifier.repository,
                }
                | verifier.keys,
            ),
        ),
        category = "verify",
        identifier = name + "/" + id,
    )
    return out
