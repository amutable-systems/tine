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

project = struct(
    is_dev = is_dev,
)
