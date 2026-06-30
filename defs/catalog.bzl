"""declare_catalog: instantiate the per-distribution target graph from a
DISTRIBUTIONS map (the @generated lock).

A consumer calls this from a BUCK file with either the cell's default catalog
(`@tine//catalog:generated.bzl`) or its own lock. The macro runs in the *consumer's*
package, so the targets it creates (engine roots, repos, distribution targets, lock generators,
and the http_files) land there and cross-reference each other package-relative
(`:name`). Cell-internal references (the format plugin, the buckify source) are
qualified `@tine//…`; bare `//` would resolve to the consumer's cell. Native rules
(http_file/filegroup) are reached through the `native` struct, since a .bzl file —
unlike a BUCK file — doesn't get them as bare globals.
"""

# `native` is implicit at evaluation time but the standalone Starlark typechecker
# doesn't model it, so load it explicitly (as the prelude itself does).
load("@prelude//:native.bzl", "native")
load("@tine//defs/rules:distribution.bzl", "distribution", "engine_root", "repo")
load("@tine//defs/rules:python.bzl", "chroot_python_binary")

def _package_format(data: dict) -> str:
    return "@tine//distribution/" + data["package_format"] + ":package_format"

def _buckify_main(data: dict) -> str:
    # The format's resolver source (a sibling of package_format).
    return "@tine//distribution/" + data["package_format"] + ":buckify.py"

def _engine_root_ref(distribution: str, data: dict) -> str:
    # The engine root that builds this distribution: its own when self-hosting (`engine`
    # is a package list), else the engine distribution its `engine` field names.
    name = data["engine"] if type(data["engine"]) == "string" else distribution
    return ":engine." + name + ".root"

# buildifier: disable=unnamed-macro  (fan-out macro: many targets, no single primary — no `name`)
def declare_catalog(distributions: dict) -> None:
    """Instantiate the per-distribution target graph from a DISTRIBUTIONS map.

    `distributions` is the @generated lock. See the module docstring for how it scopes
    into the consumer's package."""

    # The engine-provider distributions — `engine` is their own engine-root closure (a
    # list), not a reference (a name) to another distribution. Each runs the build
    # actions for every target distribution pointing at it (CentOS builds in the Fedora
    # engine).
    engine_distributions = sorted([
        name
        for name, data in distributions.items()
        if type(data["engine"]) == "list"
    ])

    for d in engine_distributions:
        # sha256-pinned http_file per engine package, named with `out` = the real
        # filename; copy = True on the filegroup because a symlink into buck-out
        # would dangle once bound into a sandbox.
        for (target, url, sha256, _) in distributions[d]["engine"]:
            native.http_file(
                name = "engine." + d + ".packages." + target,
                out = url.rsplit("/", 1)[-1],
                urls = [url],
                sha256 = sha256,
                visibility = ["PUBLIC"],
            )
        native.filegroup(
            name = "engine." + d + ".packages",
            srcs = [
                ":engine." + d + ".packages." + target
                for (target, _, _, _) in distributions[d]["engine"]
            ],
            copy = True,
            visibility = ["PUBLIC"],
        )

        # Bootstrap trampoline → the installed engine root (chroot2).
        engine_root(
            name = "engine." + d + ".root",
            packages = ":engine." + d + ".packages",
            package_format = _package_format(distributions[d]),
            visibility = ["PUBLIC"],
        )

    for d, data in distributions.items():
        # Per target distribution: its buildroot pool (one http_file per package) → a
        # local repository → a distribution target naming its engine.
        for (target, url, sha256, _) in data["packages"]:
            native.http_file(
                name = d + "." + target,
                out = url.rsplit("/", 1)[-1],
                urls = [url],
                sha256 = sha256,
                visibility = ["PUBLIC"],
            )

        # The pool built into a local repository (RepoInfo). For now one repo per
        # distribution (its whole pool); the distribution target takes a list, so more can be added
        # without touching the rule.
        repo(
            name = d + ".repo",
            id = d,
            packages = [
                ":" + d + "." + target
                for (target, _, _, _) in data["packages"]
            ],
            engine = _engine_root_ref(d, data),
            package_format = _package_format(data),
            visibility = ["PUBLIC"],
        )

        # `:<distribution>` is the distribution build target a package builds against:
        # its build drivers (plan/install/build) bound to an engine root + buildroot
        # repositories + base. The engine root is this distribution's own when it
        # self-hosts, else the engine distribution its `engine` field names.
        distribution(
            name = d,
            engine = _engine_root_ref(d, data),
            package_format = _package_format(data),
            buildroot_repositories = [":" + d + ".repo"],
            buildroot_base_packages = data["buildroot"],
            visibility = ["PUBLIC"],
        )

        # The distribution's resolver, bound to *its own* engine root (the host
        # orchestrator //distribution:buckify nests `buck run` on these to refresh
        # each fragment in the right engine). network = True: it fetches repodata.
        chroot_python_binary(
            name = d + ".buckify",
            main = _buckify_main(data),
            engine = _engine_root_ref(d, data),
            network = True,
            visibility = ["PUBLIC"],
        )
