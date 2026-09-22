# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public Git API, exported as the `git` namespace."""

load("@prelude//:rules.bzl", "git_fetch")
load("//project:defs.bzl", "project")

_MOUNT_TARGET_LABEL = "tine:mount-target"

def _checkout_impl(ctx: AnalysisContext) -> list[Provider]:
    # Keep a populated checkout as one directory source so files omitted by glob(), including dotfiles,
    # remain live inputs, but hand consumers a copy of it: the source path is the checkout itself, which
    # for a mount physically holds every tree the project ignores keep out of Buck's digest, so a consumer
    # reading it would copy build output Buck never saw. `relative_symlinks` keeps an internal symlink
    # pointing at its original relative target rather than back at the mount, so the copy survives a
    # consumer relocating it. The empty artifact preserves the implicit-checkout target for archive packages.
    tree = ctx.actions.copy_dir(ctx.attrs.out, ctx.attrs.src, relative_symlinks = True) if ctx.attrs.src else ctx.actions.symlinked_dir(ctx.attrs.out, {})
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
            labels = labels,
            out = directory,
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
    _checkout(name = name, labels = [_MOUNT_TARGET_LABEL] + labels, out = name, src = name if populated else None, **kwargs)
    return populated

git = struct(
    checkout = checkout,
    fetch = fetch,
)
