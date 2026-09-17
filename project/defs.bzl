# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Project-wide build modes, exported as the `project` namespace."""

_SECTION = "tine"
_DEV = "dev"

def _project_path(package: str, name: str, *, cell: str) -> str:
    root = read_root_config("cells", cell, "")
    return "/".join([part.strip("/") for part in (root, package, name) if part and part != "."])

def is_label(source: str) -> bool:
    """Whether `source` is a target label rather than a directory of this package."""
    return ":" in source

def _source_path(source: str) -> str:
    """Map a source target label to its mounted checkout path, e.g. `cell//pkg:app.git` to `<cell root>/pkg/app`."""
    cell = get_cell_name()
    package = package_name()
    target = source
    if is_label(source):
        path, _, target = source.rpartition(":")
        if "//" in path:
            cell, package = path.split("//", 1)
            cell = cell.removeprefix("@") or get_cell_name()
        elif path:
            package = path
    target = target.split("[", 1)[0].removesuffix(".git")
    return _project_path(package, target, cell = cell)

def is_dev(
    name: str,
    *,
    source: str | None = None,
    override: bool | None = None,
) -> bool:
    """Whether a project or the checkout selected by its source target is configured for dev mode."""
    if override != None:
        return override

    configured = [entry.strip() for entry in read_root_config(_SECTION, _DEV, "").split(",")]
    project_path = _project_path(package_name(), name, cell = get_cell_name())
    if source == None:
        return project_path in configured
    return _source_path(source) in configured

def has_files(directory: str) -> bool:
    """Whether `directory` in this package contains a file that Buck does not ignore."""

    # A glob() pattern matches a file that starts with a dot, or a file in a directory that starts with
    # a dot, only if the pattern itself contains that dot.
    return bool(glob([directory + "/**", directory + "/**/.*", directory + "/**/.*/**"]))

def populated(directory: str) -> bool:
    """Whether `directory` in this package contains a file, or is mounted for dev mode."""
    return has_files(directory) or is_dev(directory)

def kept_dir(actions: AnalysisActions, name: str) -> Artifact:
    """Declare a directory output that an action keeps across reruns, or that holds a binary."""

    # Before each run, Buck copies a kept output with a content-based path back in full. The copied
    # files get fresh timestamps. An incremental build then rebuilds everything. For an output that the
    # driver empties first, the copy is wasted work.
    # The path is not content-based outside dev mode either. If the kind of path changed with dev mode,
    # a switch would leave the output of the old kind at this path. A kept action never removes that
    # output.
    return actions.declare_output(name, dir = True, has_content_based_path = False)

def copy_source(actions: AnalysisActions, name: str, src: Artifact) -> Artifact:
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
    return actions.copy_dir(name, src, has_content_based_path = False, preserve_mtimes = True, relative_symlinks = True)

def _source_impl(ctx: AnalysisContext) -> list[Provider]:
    name = ctx.label.name.removesuffix(".src")

    # A directory that contains no file becomes an empty directory. The consumer then fails with its
    # own error about the missing sources, rather than Buck failing on a missing source path.
    tree = copy_source(ctx.actions, name, ctx.attrs.src) if ctx.attrs.src else ctx.actions.symlinked_dir(name, {})
    return [DefaultInfo(default_output = tree)]

_source = rule(
    doc = "Copy a directory of this package for the rules that build from it.",
    impl = _source_impl,
    attrs = {
        "src": attrs.option(attrs.source(allow_directory = True), default = None, doc = "the directory, unset when it contains no file"),
    },
)

def source(src: str) -> str:
    """Return the label of a source tree for a build rule.

    A label is returned unchanged. A directory of this package is replaced by a target that copies the
    files Buck digested.
    """
    if is_label(src):
        return src
    name = src + ".src"

    # Several targets can build from one directory. They share one copy of it.
    if not rule_exists(name):
        _source(name = name, src = src if populated(src) else None)
    return ":" + name

project = struct(
    copy_source = copy_source,
    has_files = has_files,
    is_dev = is_dev,
    is_label = is_label,
    kept_dir = kept_dir,
    populated = populated,
    source = source,
)
