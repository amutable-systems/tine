"""The public Git API, exported as the `git` namespace."""

load("@prelude//:rules.bzl", "git_fetch")
load("//project:defs.bzl", "project")

_MOUNT_TARGET_LABEL = "tine:mount-target"

def _checkout_impl(ctx: AnalysisContext) -> list[Provider]:
    # `src` is one directory source rather than a glob(), which would drop its dotfiles.
    name = ctx.label.name.removesuffix(".git")
    if ctx.attrs.src == None:
        tree = ctx.actions.symlinked_dir(name, {})
    elif ctx.attrs.copy:
        tree = project.digested(ctx.actions, name, ctx.attrs.src)
    else:
        # A fetch override stays live: an upstream tree is large, and its timestamps drive incremental
        # builds. It keeps its source identity, which is how consumers keep its results out of the
        # shared cache.
        tree = ctx.attrs.src
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
        "copy": attrs.bool(doc = "hand consumers the copy Buck digested rather than the live directory"),
        "labels": attrs.list(attrs.string(), default = [], doc = "labels used to query this target"),
        "src": attrs.option(attrs.source(allow_directory = True), default = None, doc = "the populated checkout"),
        "sub_targets": attrs.list(attrs.string(), default = [], doc = "tree paths exposed as sub-targets"),
    },
)

def fetch(name: str, repo: str, rev: str, sub_targets: list[str] = [], visibility: list[str] | None = None, labels: list[str] = [], **kwargs) -> None:
    """Fetch `rev` unless this package contains a non-empty checkout named after the target."""
    directory = name.removesuffix(".git")
    labels = [_MOUNT_TARGET_LABEL] + labels
    srcs = glob([directory + "/**"])
    if srcs or project.is_dev(directory):
        _checkout(
            name = name,
            copy = False,
            labels = labels,
            src = directory,
            sub_targets = sub_targets,
            visibility = visibility,
        )
    else:
        git_fetch(name = name, repo = repo, rev = rev, sub_targets = sub_targets, visibility = visibility, labels = labels, **kwargs)

def checkout(name: str, labels: list[str] = [], **kwargs) -> bool:
    """Expose the directory named after the target as a tree, empty until committed or mounted.

    Returns whether it currently holds anything.
    """
    srcs = glob([name + "/**"])
    populated = bool(srcs) or project.is_dev(name)
    _checkout(name = name, copy = True, labels = [_MOUNT_TARGET_LABEL] + labels, src = name if populated else None, **kwargs)
    return populated

git = struct(
    checkout = checkout,
    fetch = fetch,
)
