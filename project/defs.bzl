# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Project-wide build modes, exported as the `project` namespace."""

_SECTION = "tine"
_DEV = "dev"

def _project_path(package: str, name: str, *, cell: str) -> str:
    root = read_root_config("cells", cell, "")
    return "/".join([part.strip("/") for part in (root, package, name) if part and part != "."])

def _source_path(source: str) -> str:
    """Map a source target label to its mounted checkout path, e.g. `cell//pkg:app.git` to `<cell root>/pkg/app`."""
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

def kept_dir(actions: AnalysisActions, name: str) -> Artifact:
    """Declare a directory output that an action keeps across reruns, or that holds a binary."""

    # Before each run, Buck copies a kept output with a content-based path back in full. The copied
    # files get fresh timestamps. An incremental build then rebuilds everything. For an output that the
    # driver empties first, the copy is wasted work.
    # The path is not content-based outside dev mode either. If the kind of path changed with dev mode,
    # a switch would leave the output of the old kind at this path. A kept action never removes that
    # output.
    return actions.declare_output(name, dir = True, has_content_based_path = False)

project = struct(
    is_dev = is_dev,
    kept_dir = kept_dir,
)
