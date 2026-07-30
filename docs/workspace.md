# Shared development workspace

Tine can register several independent repositories as cells of one parent Buck project. Buck then stores all
local state and outputs in the parent's single `buck-out`, while each repository keeps its own BUCK files and
cell-local configuration.

Initialize the parent from the checkout containing tine:

```console
$ bin/tine workspace init ~/Projects
```

This registers the current checkout, creates `.tine/workspace.toml` and the parent Buck configuration, and
writes `.config/mise/conf.d/tine.toml`. Run `mise trust ~/Projects` once, then start a new shell under the
workspace. `tine` is available on `PATH` from any child directory.

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
fragment. Projects must not contain `.buckroot`, because it would stop Buck before it reaches the shared
workspace configuration. Project `.buckconfig` files remain project-owned and unchanged.
