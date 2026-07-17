"""Create and maintain a shared Buck workspace for tine projects."""

import argparse
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from errors import CliError
from util import ANSI_CYAN, ANSI_GREEN, ANSI_RESET, atomic_write_text

MARKER = "# Managed by tine workspace. Edit with `tine workspace ...`."
MANIFEST_VERSION = 2
RESERVED_CELLS = frozenset({"config", "fbsource", "none", "prelude", "root", "tine", "toolchains"})


@dataclass(frozen=True)
class Project:
    name: str
    path: Path


@dataclass(frozen=True)
class Manifest:
    workspace: Path
    tine: Path
    projects: tuple[Project, ...]


def _path_within(path: Path, parent: Path, description: str) -> Path:
    try:
        return path.relative_to(parent)
    except ValueError as error:
        raise CliError(f"{description} must be inside the workspace: {path}") from error


def _absolute(path: Path, base: Path) -> Path:
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _cell_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not name:
        raise CliError(f"cannot derive a cell name from directory {value!r}")
    if name[0].isdigit():
        name = f"project_{name}"
    if name in RESERVED_CELLS:
        raise CliError(f"directory {value!r} produces reserved cell name {name!r}")
    return name


def _buck_output(buck: Path, cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        [str(buck), *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CliError(f"Buck failed in {cwd}: {detail}")
    return result.stdout.strip()


def _audit_cells(buck: Path, cwd: Path) -> dict[str, Path]:
    try:
        raw = json.loads(_buck_output(buck, cwd, "audit", "cell", "--json"))
    except json.JSONDecodeError as error:
        raise CliError("Buck returned invalid JSON for the cell map") from error
    if not isinstance(raw, dict):
        raise CliError("Buck returned invalid data for the cell map")
    result: dict[str, Path] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise CliError("Buck returned invalid data for the cell map")
        result[key] = Path(value).resolve()
    return result


def _required_cell(cells: dict[str, Path], name: str) -> Path:
    try:
        return cells[name]
    except KeyError as error:
        raise CliError(f"the source checkout has no {name!r} cell") from error


def _check_managed(path: Path) -> None:
    if path.exists() and not path.is_file():
        raise CliError(f"managed path is not a file: {path}")
    if path.exists() and not path.read_text().startswith(f"{MARKER}\n"):
        raise CliError(f"refusing to replace unmanaged file: {path}")


def _relative(path: Path, workspace: Path, description: str) -> str:
    return _path_within(path, workspace, description).as_posix()


def _render_manifest(manifest: Manifest) -> str:
    lines = [
        'managed_by = "tine"',
        f"version = {MANIFEST_VERSION}",
        f"tine = {json.dumps(_relative(manifest.tine, manifest.workspace, 'tine cell'))}",
        "",
        "[projects]",
    ]
    for project in sorted(manifest.projects, key=lambda item: item.name):
        lines.extend(("", f"[projects.{json.dumps(project.name)}]"))
        lines.append(f"path = {json.dumps(_relative(project.path, manifest.workspace, 'project'))}")
    return "\n".join(lines) + "\n"


def _manifest_value_path(value: object, key: str, workspace: Path) -> Path:
    if not isinstance(value, dict):
        raise CliError("invalid workspace manifest")
    item = value.get(key)
    if not isinstance(item, str):
        raise CliError(f"workspace manifest has no valid {key!r} path")
    return _absolute(Path(item), workspace)


def _load_manifest(workspace: Path) -> Manifest:
    path = workspace / ".tine/workspace.toml"
    if not path.is_file():
        raise CliError(f"no tine workspace manifest found at {path}")
    try:
        value = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as error:
        raise CliError(f"invalid tine workspace manifest {path}: {error}") from error
    if not isinstance(value, dict) or value.get("managed_by") != "tine":
        raise CliError(f"invalid tine workspace manifest: {path}")
    if value.get("version") != MANIFEST_VERSION:
        raise CliError(f"unsupported tine workspace manifest version in {path}")
    project_values = value.get("projects")
    if not isinstance(project_values, dict):
        raise CliError(f"invalid project map in {path}")
    projects: list[Project] = []
    for name, project_value in project_values.items():
        if not isinstance(name, str) or not isinstance(project_value, dict):
            raise CliError(f"invalid project entry in {path}")
        project_path = _manifest_value_path(project_value, "path", workspace)
        projects.append(Project(name, project_path))
    return Manifest(
        workspace,
        _manifest_value_path(value, "tine", workspace),
        tuple(projects),
    )


def _cell_entries(manifest: Manifest) -> list[tuple[str, Path]]:
    entries = [
        ("root", manifest.workspace),
        ("tine", manifest.tine),
        ("toolchains", manifest.tine / "toolchains"),
        ("prelude", manifest.workspace / "prelude"),
        ("none", manifest.workspace / "none"),
    ]
    for project in sorted(manifest.projects, key=lambda item: item.name):
        entries.append((project.name, project.path))
    return entries


def _render_buckconfig(manifest: Manifest) -> str:
    entries = _cell_entries(manifest)
    cells = "\n".join(
        f"{name} = {_relative(path, manifest.workspace, f'{name} cell') or '.'}" for name, path in entries
    )
    detectors = " ".join(
        f"target:{name}//...->prelude//platforms:default"
        for name, _path in entries
        if name not in {"none", "prelude", "toolchains"}
    )
    return f"""{MARKER}

[cells]
{cells}

[cell_aliases]
config = prelude
fbsource = none

[external_cells]
prelude = bundled

[parser]
target_platform_detector_spec = {detectors}

[build]
execution_platforms = prelude//platforms:default

[project]
# The root is only a registry. Registered cells apply their own ignore rules.
ignore = ?*

[buck2]
materializations = deferred
sqlite_materializer_state = true
defer_write_actions = true
"""


def _render_mise(manifest: Manifest) -> str:
    bin_directory = _relative(manifest.tine / "bin", manifest.workspace, "tine commands")
    tools = _relative(manifest.tine / "tools", manifest.workspace, "tine tools")
    return f"""{MARKER}

[env]
_.path = ["{{{{config_root}}}}/{bin_directory}", "{{{{config_root}}}}/{tools}"]
"""


def _assert_project(project: Project, manifest: Manifest) -> None:
    _path_within(project.path, manifest.workspace, "project")
    if not project.path.is_dir():
        raise CliError(f"project directory does not exist: {project.path}")
    if (project.path / ".buckroot").exists():
        raise CliError(f"remove {project.path / '.buckroot'} before adding the project")


def _write_workspace(manifest: Manifest) -> None:
    for description, path in (
        ("tine cell", manifest.tine),
        ("toolchains cell", manifest.tine / "toolchains"),
    ):
        _path_within(path, manifest.workspace, description)
        if not path.is_dir():
            raise CliError(f"{description} does not exist: {path}")
    for project in manifest.projects:
        _assert_project(project, manifest)
    managed = {
        manifest.workspace / ".buckconfig": _render_buckconfig(manifest),
        manifest.workspace / ".config/mise/conf.d/tine.toml": _render_mise(manifest),
    }
    for path in managed:
        _check_managed(path)
    buckroot = manifest.workspace / ".buckroot"
    if buckroot.exists() and not buckroot.is_file():
        raise CliError(f"workspace Buck marker is not a file: {buckroot}")
    if not buckroot.exists():
        atomic_write_text(buckroot, "", mode=0o644)
    for path, content in managed.items():
        atomic_write_text(path, content, mode=0o644)
    atomic_write_text(manifest.workspace / ".tine/workspace.toml", _render_manifest(manifest), mode=0o644)


def find_workspace(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".tine/workspace.toml").is_file():
            return candidate
    raise CliError(f"no tine workspace found above {start}")


def require_project(path: Path) -> Project:
    path = path.resolve()
    manifest = _load_manifest(find_workspace(path))
    projects = [
        project for project in manifest.projects if path == project.path or project.path in path.parents
    ]
    if not projects:
        raise CliError(
            f"{path} is not in a registered project in {manifest.workspace}; "
            "run `tine workspace add <project-directory>` first"
        )
    return max(projects, key=lambda project: len(project.path.parts))


def _validate(manifest: Manifest, buck: Path) -> None:
    expected = dict(_cell_entries(manifest))
    actual = _audit_cells(buck, manifest.workspace)
    failures = [
        f"{name}: expected {path}, got {actual.get(name)}"
        for name, path in expected.items()
        if actual.get(name) != path
    ]
    for project in manifest.projects:
        root = Path(_buck_output(buck, project.path, "root", "--kind", "project")).resolve()
        if root != manifest.workspace:
            failures.append(f"{project.name}: Buck project root is {root}, expected {manifest.workspace}")
    if failures:
        raise CliError("workspace validation failed:\n  " + "\n  ".join(failures))


def _new_project(
    path: Path,
    base: Path,
) -> Project:
    project_path = _absolute(path, base)
    project_name = _cell_name(project_path.name)
    return Project(project_name, project_path)


def _init(args: argparse.Namespace) -> Manifest:
    workspace = _absolute(args.directory, args.caller_directory)
    workspace.mkdir(parents=True, exist_ok=True)
    manifest_path = workspace / ".tine/workspace.toml"
    if manifest_path.exists():
        manifest = _load_manifest(workspace)
        _write_workspace(manifest)
        _validate(manifest, args.buck)
        return manifest

    source_root = args.source_root.resolve()
    if source_root == workspace:
        raise CliError("the workspace must be a parent of the tine source checkout")
    _path_within(source_root, workspace, "tine source checkout")
    if (source_root / ".buckroot").exists():
        raise CliError(f"remove {source_root / '.buckroot'} before initializing the workspace")
    cells = _audit_cells(args.buck, source_root)
    project = _new_project(source_root, args.caller_directory)
    manifest = Manifest(
        workspace,
        _required_cell(cells, "tine"),
        (project,),
    )
    _write_workspace(manifest)
    _validate(manifest, args.buck)
    return manifest


def _add(args: argparse.Namespace) -> Manifest:
    project_path = _absolute(args.directory, args.caller_directory)
    workspace = find_workspace(project_path)
    manifest = _load_manifest(workspace)
    project = _new_project(project_path, args.caller_directory)
    by_name = {item.name: item for item in manifest.projects}
    for item in manifest.projects:
        if item.path == project.path and item.name != project.name:
            raise CliError(f"{project.path} is already registered as cell {item.name!r}")
    existing = by_name.get(project.name)
    if existing is not None and existing.path != project.path:
        raise CliError(f"cell {project.name!r} already points to {existing.path}")
    by_name[project.name] = project
    updated = Manifest(
        manifest.workspace,
        manifest.tine,
        tuple(by_name.values()),
    )
    _write_workspace(updated)
    _validate(updated, args.buck)
    return updated


def _remove(args: argparse.Namespace) -> Manifest:
    project_path = _absolute(args.directory, args.caller_directory)
    workspace = find_workspace(project_path)
    manifest = _load_manifest(workspace)
    project = next((item for item in manifest.projects if item.path == project_path), None)
    if project is None:
        raise CliError(f"{project_path} is not a registered project in {workspace}")
    updated = Manifest(
        manifest.workspace,
        manifest.tine,
        tuple(item for item in manifest.projects if item != project),
    )
    _write_workspace(updated)
    _validate(updated, args.buck)
    return updated


def _doctor(args: argparse.Namespace) -> Manifest:
    manifest = _load_manifest(find_workspace(args.caller_directory))
    expected_files = {
        manifest.workspace / ".buckconfig": _render_buckconfig(manifest),
        manifest.workspace / ".config/mise/conf.d/tine.toml": _render_mise(manifest),
    }
    for path, expected in expected_files.items():
        if not path.is_file():
            raise CliError(f"missing generated workspace file: {path}")
        if path.read_text() != expected:
            raise CliError(f"generated workspace file is stale: {path}")
    _validate(manifest, args.buck)
    return manifest


def _list(args: argparse.Namespace) -> None:
    manifest = _load_manifest(find_workspace(args.caller_directory))
    projects = sorted(manifest.projects, key=lambda item: item.name)
    width = max((len(project.name) for project in projects), default=0)

    print(f"{ANSI_CYAN}Workspace{ANSI_RESET}  {manifest.workspace}")
    print(f"{ANSI_CYAN}Projects{ANSI_RESET}")
    for project in projects:
        name = f"{project.name:<{width}}"
        print(f"  {ANSI_GREEN}{name}{ANSI_RESET}  {project.path}")


def add_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    workspace = commands.add_parser("workspace", help="manage a shared Buck workspace")
    workspace.set_defaults(handler=run)
    workspace_commands = workspace.add_subparsers(dest="workspace_command", required=True)

    init = workspace_commands.add_parser("init", help="initialize a shared workspace")
    init.add_argument("directory", type=Path)
    init.set_defaults(workspace_handler=_init)

    add = workspace_commands.add_parser("add", help="register a project cell")
    add.add_argument("directory", type=Path)
    add.set_defaults(workspace_handler=_add)

    remove = workspace_commands.add_parser("remove", help="unregister a project cell")
    remove.add_argument("directory", type=Path)
    remove.set_defaults(workspace_handler=_remove)

    list_projects = workspace_commands.add_parser("list", help="list registered projects")
    list_projects.set_defaults(handler=_list)

    doctor = workspace_commands.add_parser("doctor", help="validate workspace configuration")
    doctor.set_defaults(workspace_handler=_doctor)


def run(args: argparse.Namespace) -> None:
    manifest = args.workspace_handler(args)
    print(f"Tine workspace ready at {manifest.workspace}")
    print(f"Registered projects: {', '.join(project.name for project in manifest.projects)}")
