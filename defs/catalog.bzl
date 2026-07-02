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
load("@tine//defs/rules:distribution.bzl", "distribution", "remote_repository")
load("@tine//defs/rules:engine.bzl", "engine")

def _package_format(data: dict) -> str:
    return "@tine//distribution/" + data["package_format"] + ":package_format"

def _buckify_driver(data: dict) -> str:
    # The format's resolver driver (a sibling of package_format).
    return "@tine//distribution/" + data["package_format"] + ":buckify"

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
        for (target, url, sha256, size, _) in distributions[d]["engine"]:
            native.http_file(
                name = "engine." + d + ".packages." + target,
                out = url.rsplit("/", 1)[-1],
                urls = [url],
                sha256 = sha256,
                size_bytes = size,  # from repodata — skips buck's HEAD size probe
                visibility = ["PUBLIC"],
            )
        native.filegroup(
            name = "engine." + d + ".packages",
            srcs = [
                ":engine." + d + ".packages." + target
                for (target, _, _, _, _) in distributions[d]["engine"]
            ],
            copy = True,
            visibility = ["PUBLIC"],
        )

        # Bootstrap trampoline → the installed engine root (chroot2).
        engine(
            name = "engine." + d + ".root",
            packages = ":engine." + d + ".packages",
            package_format = _package_format(distributions[d]),
            visibility = ["PUBLIC"],
        )

    for d, data in distributions.items():
        # Per target distribution: its upstream buildroot repos (pinned repodata → a
        # remote_repository each) → a distribution target naming its engine. The buildroot's
        # resolved rpms are fetched lazily at build time, so only repodata is pinned here.
        repo_targets = []
        for r in data.get("buildroot_repos", []):  # not yet present before first refresh-catalog
            for f in r["streams"]:
                native.http_file(
                    name = "{}.{}.{}".format(d, r["id"], f["out"]),
                    out = f["out"],
                    urls = [f["url"]],
                    sha256 = f["sha256"],
                    size_bytes = f["size"],  # from repomd — skips buck's HEAD size probe
                    visibility = ["PUBLIC"],
                )
            rt = "{}.{}.repo".format(d, r["id"])
            remote_repository(
                name = rt,
                id = r["id"],
                baseurl = r["baseurl"],
                repomd = r["repomd"],
                streams = [":{}.{}.{}".format(d, r["id"], f["out"]) for f in r["streams"]],
                visibility = ["PUBLIC"],
            )
            repo_targets.append(":" + rt)

        # `:<distribution>` is the distribution build target a package builds against:
        # its build drivers (plan/download/install/build) bound to an engine root +
        # buildroot repositories + base. The engine root is this distribution's own when it
        # self-hosts, else the engine distribution its `engine` field names.
        distribution(
            name = d,
            engine = _engine_root_ref(d, data),
            package_format = _package_format(data),
            buildroot_repositories = repo_targets,
            buildroot_base_packages = data["buildroot"],
            visibility = ["PUBLIC"],
        )

        # The distribution's resolver, bound to *its own* engine root (the host
        # orchestrator //distribution:buckify nests `buck run` on these to refresh
        # each fragment in the right engine). The engine root's RunInfo is the
        # enter-the-engine prefix; --network (it fetches repodata) goes before the
        # `--` that starts the command.
        native.command_alias(
            name = d + ".buckify",
            exe = _engine_root_ref(d, data),
            args = [
                "--network",
                "--",
                "$(location {})".format(_buckify_driver(data)),
            ],
            visibility = ["PUBLIC"],
        )
