# Shared development workspace

Tine can register several independent repositories as cells of one parent Buck project. Buck then stores all
local state and outputs in the parent's single `buck-out`, while each repository keeps its own BUCK files and
cell-local configuration.

Initialize the parent from this checkout:

```console
$ bin/tine workspace init ~/Projects
```

This creates `.tine/workspace.toml` and the parent Buck configuration, and writes
`.config/mise/conf.d/tine.toml`. Run `mise trust ~/Projects` once, then start a new shell under the
workspace. `tine` is available on `PATH` from any child directory.

This checkout is registered like any other project, and because it is named `tine` its cell *is* the
workspace's `tine` cell, which is how other projects resolve `tine//...` against it. Nothing else may claim
that name. If the cell is instead vendored inside a larger project, that project is registered under its own
name and supplies `tine` as a nested cell.

No project may contain a `.buckroot`, this one included. Buck takes the *furthest* ancestor `.buckconfig`
and `.buckroot` stops that search, so a project carrying one never reaches the workspace root. `init` and
`add` both refuse it.

Register another checkout explicitly:

```console
$ cd ~/Projects/my-project
$ tine workspace add .
```

The cell name is derived from the normalized directory name. Use cell-relative `//...` labels for targets in
the current project; they work both when the project is standalone and when it is registered in a shared
workspace. Run `tine workspace list` from anywhere inside the workspace to show its registered projects.

Unregister a project by its directory:

```console
$ tine workspace remove ~/Projects/my-project
```

Removing a project updates only the workspace registry and generated parent configuration; it does not
remove or modify the project directory.

Projects use `tine//catalog:...` for Tine's default catalog. A project-specific catalog is an ordinary
`//catalog` package within that project and does not require workspace registration. Refresh one with
`tools/buck run tine//tools:refresh-catalog -- my_project//catalog`, using the project's cell name.

The workspace root is only a cell registry. Its `?*` ignore glob matches every non-empty root-relative path,
but not the workspace root itself. Consequently, an unregistered directory is invisible to Buck and cannot
invalidate the shared file watcher. A registered path is first mapped into its own cell and therefore uses
that cell's `[project] ignore` setting instead. Run `tine workspace doctor` to check the manifest, generated
files, cell map, and Buck project boundary.

Generated files carry a marker. The CLI refuses to replace an unmarked workspace `.buckconfig` or mise
fragment. Project `.buckconfig` files remain project-owned and unchanged.

A generated `.buckconfig` whose manifest has been deleted is orphaned: it still captures the Buck project
root, so every command resolves against a cell map describing nothing. `init` discards such a file before
re-reading the cell map. The manifest is the registry, so recovering this way starts with no projects and
each one has to be added again. If the orphaned map is broken badly enough that Buck cannot run at all,
delete the workspace's `.buckconfig` by hand first, since the CLI itself runs through Buck.
