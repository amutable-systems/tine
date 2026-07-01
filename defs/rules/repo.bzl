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
    },
)

def _download_closure(actions: AnalysisActions, tx: ArtifactValue, closure: OutputArtifact) -> list[Provider]:
    # Dynamic: the resolved set is only known once the transaction exists. Fetch rpms into
    # content-based output for global sharing. The closure is a symlink tree.
    entries = tx.read_json()

    # The engine lock doubles as a transaction and its unlocked seed is `{}` (an object,
    # not a list); fail loudly instead of downloading an empty closure.
    if type(entries) != type([]):
        fail("transaction is not a list (an engine lock still seeded `{}`?); run refresh-catalog")

    rpms = {}
    for entry in entries:
        name = entry["url"].rsplit("/", 1)[-1]
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
    },
)

def download_closure(ctx: AnalysisContext, tx: Artifact) -> Artifact:
    """Download the transaction `tx` ([{url, sha256, size}]) into a symlink-tree dir.

    Lazy (only this closure's subset), shared (each rpm downloaded once, content-addressed),
    dynamic (the resolved set isn't known until the transaction is built)."""
    closure = ctx.actions.declare_output("install.closure", dir = True)
    ctx.actions.dynamic_output_new(_download(tx = tx, closure = closure.as_output()))
    return closure
