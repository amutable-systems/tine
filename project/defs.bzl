"""Project-wide build modes, exported as the `project` namespace."""

_SECTION = "tine"
_DEV = "dev"

def _project_path(package: str, name: str, *, cell: str) -> str:
    root = read_root_config("cells", cell, "")
    return "/".join([part.strip("/") for part in (root, package, name) if part and part != "."])

def _source_path(source: str) -> str:
    """Map a source target label to its mounted checkout path, e.g. `cell//pkg:app.git` to `<cell root>/pkg/app`."""
    if "//" in source and ":" not in source:
        # `//pkg` is short for `//pkg:pkg`.
        source += ":" + source.rpartition("/")[2]
    path, separator, target = source.rpartition(":")
    cell = get_cell_name()
    package = package_name()
    if separator and "//" in path:
        cell, package = path.split("//", 1)
        cell = cell.removeprefix("@") or get_cell_name()
    elif separator and path:
        package = path
    elif not separator:
        target = source
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

def is_directory(source: str | None) -> bool:
    """Whether a source names a directory of the current package rather than a target."""
    return source == None or not (source.startswith(":") or "//" in source)

def digested(actions: AnalysisActions, name: str, tree: Artifact) -> Artifact:
    """Copy the files of a source directory that Buck digested."""

    # The directory itself also holds whatever project ignores keep out of Buck's digest, so an action
    # reading it could use build output Buck never hashed. `relative_symlinks` keeps an internal link
    # pointing at its original relative target, so the copy survives being relocated.
    # Not content-based: reverting an edit would bring back the earlier copy, timestamps included, and an
    # incremental build would take the reverted file for unchanged.
    return actions.copy_dir(name, tree, has_content_based_path = False, relative_symlinks = True)

project = struct(
    digested = digested,
    is_dev = is_dev,
    is_directory = is_directory,
)
