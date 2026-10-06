# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public Git API, exported as the `git` namespace."""

load("@prelude//:rules.bzl", "git_fetch")
load("//project:defs.bzl", "project")

_MOUNT_TARGET_LABEL = "tine:mount-target"

def _digested(actions: AnalysisActions, name: str, tree: Artifact) -> Artifact:
    """Copy the files of a source directory that are in Buck's digest of the directory."""

    # The source directory also holds files that the ignore rules of the project keep out of Buck's
    # digest, such as build output. An action that reads the source directory could use files that Buck
    # never hashed.
    # `relative_symlinks` keeps a symlink inside the tree pointing at its relative target. The copy then
    # still works after a consumer moves it.
    # `preserve_mtimes` keeps the timestamps of the source files. An incremental build compares
    # timestamps to find changed files. A fresh copy would give every file a new timestamp. Only a local
    # copy keeps the timestamps. An incremental build only sees local copies, because Buck never shares
    # the results of an incremental build.
    # `has_content_based_path = False` gives the copy a fixed path. With a content-based path, reverting
    # an edit would bring back the earlier copy with its earlier timestamps. An incremental build would
    # then treat the reverted file as unchanged.
    # The key of the copy action does not include `relative_symlinks`, `preserve_mtimes` or
    # `has_content_based_path`. A change to a flag takes effect only after `buck2 clean`.
    return actions.copy_dir(name, tree, has_content_based_path = False, preserve_mtimes = True, relative_symlinks = True)

def _checkout_impl(ctx: AnalysisContext) -> list[Provider]:
    # `src` is a single directory source rather than a glob(), because a glob() drops dotfiles. Archive
    # packages depend on the implicit checkout target, so an unpopulated checkout outputs an empty
    # directory.
    tree = _digested(ctx.actions, ctx.attrs.out, ctx.attrs.src) if ctx.attrs.src else ctx.actions.symlinked_dir(ctx.attrs.out, {})
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
    if project.populated(directory):
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
    populated = project.populated(name)
    _checkout(name = name, labels = [_MOUNT_TARGET_LABEL] + labels, out = name, src = name if populated else None, **kwargs)
    return populated

def source(name: str, directory: str) -> str:
    """Declare a copy of `directory` in this package for a build rule, and return its label.

    Unlike `checkout()`, `source()` does not label the target as a mount target. A mount of
    `directory` goes to the path of `directory` itself.
    """
    populated = project.populated(directory)
    _checkout(name = name, out = name, src = directory if populated else None)
    return ":" + name

git = struct(
    checkout = checkout,
    fetch = fetch,
    source = source,
)
