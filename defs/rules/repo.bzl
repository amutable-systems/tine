"""Repositories: what they are (RepoInfo) and how resolved packages are fetched from them."""

RepoInfo = provider(
    # `dir` holds the `repodata/` the plan resolves against. Two flavors:
    #   - remote: `dir` is just pinned repodata (no packages); `baseurl` is the real remote base,
    #     so plan turns each resolved package's location into a download URL. `packages` is empty —
    #     the resolved subset is fetched lazily at build time.
    #   - local: `dir` is a createrepo'd tree with the rpms present; `packages` are those artifacts
    #     (keyed by basename to scope an install); `baseurl` is "".
    doc = "A package repository.",
    fields = {
        "id": provider_field(str),
        "dir": provider_field(Artifact),
        "packages": provider_field(list[Artifact]),
        "manifest": provider_field(typing.Any, default = None),  # the authored {id, baseurl} JSON (remote repos; refresh input)
        "baseurl": provider_field(str, default = ""),
        # dnf semantics: lower number wins; 99 is libdnf's default.
        "priority": provider_field(int, default = 99),
    },
)

def _download_closure(actions: AnalysisActions, tx: ArtifactValue, closure: OutputArtifact, extra_packages: list[Artifact]) -> list[Provider]:
    # Dynamic: the resolved set is only known once the transaction exists. Each rpm is fetched
    # from the remote repo (https → content-addressed download_file, shared across closures) or
    # taken from our own builds (file:// → already local, projected in place). The closure is a
    # symlink tree.
    entries = tx.read_json()

    # The engine lock doubles as a transaction and its unlocked seed is `{}` (an object,
    # not a list); fail loudly instead of downloading an empty closure.
    if type(entries) != type([]):
        fail("transaction is not a list (an engine lock still seeded `{}`?); run refresh-catalog")

    rpms = {}
    for entry in entries:
        name = entry["url"].rsplit("/", 1)[-1]
        if entry["url"].startswith("file://"):
            # An extra-packages rpm: its location_href is <dir-index>/<name> (see createrepo's
            # --packages-dir), naming the originating rpms dir — the repo's own tree is a promise
            # artifact, which buck can't project.
            idx = entry["url"].rsplit("/", 2)[-2]
            rpms[name] = extra_packages[int(idx)].project(name)
        else:
            out = actions.declare_output("rpm", name, has_content_based_path = True)
            actions.download_file(out, entry["url"], sha256 = entry["sha256"], size_bytes = entry["size"])
            rpms[name] = out
    actions.symlinked_dir(closure, rpms)
    return []

_download = dynamic_actions(
    impl = _download_closure,
    attrs = {
        "tx": dynattrs.artifact_value(),
        "closure": dynattrs.output(),
        "extra_packages": dynattrs.value(list[Artifact]),  # our-build rpms dirs (may be empty)
    },
)

def download_closure(ctx: AnalysisContext, tx: Artifact, extra_packages: list[Artifact] = []) -> Artifact:
    """Download the transaction `tx` ([{url, sha256, size}]) into a symlink-tree dir.

    Lazy (only this closure's subset), shared (each rpm downloaded once, content-addressed),
    dynamic (the resolved set isn't known until the transaction is built). `extra_packages`
    is the rpms dirs of our own builds; their rpms resolve as file:// URLs and are projected
    in place instead of fetched."""
    closure = ctx.actions.declare_output("install.closure", dir = True)
    ctx.actions.dynamic_output_new(_download(
        tx = tx,
        closure = closure.as_output(),
        extra_packages = extra_packages,
    ))
    return closure
