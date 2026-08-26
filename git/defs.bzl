"""The public Git API, exported as the `git` namespace."""

load("@prelude//:rules.bzl", "git_fetch")

_MOUNT_TARGET_LABEL = "tine:mount-target"

def _checkout_impl(ctx: AnalysisContext) -> list[Provider]:
    # Consumers copy directory artifacts into scratch space before modifying them, so this rule can avoid
    # an unnecessary copy by assembling the artifact from symlinks.
    tree = ctx.actions.symlinked_dir(ctx.attrs.out, ctx.attrs.srcs)
    return [
        DefaultInfo(
            default_output = tree,
            sub_targets = {path: [DefaultInfo(default_output = tree.project(path))] for path in ctx.attrs.sub_targets},
        ),
    ]

_checkout = rule(
    doc = "Expose a checkout in the current package as a git_fetch-compatible tree.",
    impl = _checkout_impl,
    attrs = {
        "labels": attrs.list(attrs.string(), default = [], doc = "labels used to query this target"),
        "out": attrs.string(doc = "name of the fetched work tree"),
        "srcs": attrs.dict(attrs.string(), attrs.source(), doc = "checkout files keyed by relative path"),
        "sub_targets": attrs.list(attrs.string(), default = [], doc = "tree paths exposed as sub-targets"),
    },
)

def fetch(name: str, repo: str, rev: str, sub_targets: list[str] = [], visibility: list[str] | None = None, labels: list[str] = [], **kwargs) -> None:
    """Fetch `rev` unless this package contains a non-empty checkout named after the target."""
    checkout = name.removesuffix(".git")
    labels = [_MOUNT_TARGET_LABEL] + labels
    srcs = glob([checkout + "/**"])
    if srcs:
        _checkout(
            name = name,
            labels = labels,
            out = checkout,
            srcs = {src.removeprefix(checkout + "/"): src for src in srcs},
            sub_targets = sub_targets,
            visibility = visibility,
        )
    else:
        git_fetch(name = name, repo = repo, rev = rev, sub_targets = sub_targets, visibility = visibility, labels = labels, **kwargs)

git = struct(
    fetch = fetch,
)
