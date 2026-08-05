# AGENTS.md

## General

- **Never touch `buck-out/` directly** — no `rm`, `mv`, edits, or reads-for-mutation. Force a
  rebuild with buck (`buck clean`, then rebuild the target); to inspect an output, get its path
  from `buck build --show-output`/`--out` and read it read-only.
- **Never `buck build | tail`** — redirect full output to a log file, show the path so that the user can
  follow along, and check exit separately. Inspect failed actions with
  `tools/buck log what-ran --failed --show-std-err`.
- Line break documents and plans at 109 columns.
- Keep comments concise; say each thing once. Comments explain *why*, not *what*.

## Layout

The `tine` cell contains reusable machinery organized by subsystem. Starlark rules and their
drivers live together in `engine`, `package`, `package_system`, `cargo`, `go`, `image`, `image_format`,
`rootfs`, and `archive`. Vendored code lives in `vendor`. The default catalog is `tine//catalog`;
consumers may instead declare a project-specific `//catalog` package. `examples` holds the demo images
that CI builds and boot-tests, plus a dev box. Package sources and targets live in the OS.git root that
consumes this cell, under `packages/` (built as `//packages/...`).

Unit tests sit beside the driver they exercise as `<driver>_test.py`, run by an `engine_unittest`
target in the same package (`tine//engine:test.bzl`), which only `buck test` runs. Cross-package
sources reach a suite through `deps` on a `python_bootstrap_library`, never `export_file`. `tests`
holds only the drivers shared across images, like the VM boot smoke.

## Commands

The pinned `buck` lives in `tools/`,; invoke it by path. Everything else (python3, ruff, ty, starlark_fmt)
is pinned in `tools/BUCK` and fetched by buck itself; the dev commands are `buck run` targets.

- **buck (whole project):** `buck build //...` — builds everything but runs no tests; see below.
- **Lint (format, lint, type-check):** `buck run tine//tools:lint`
- **Check (lint plus the hooked-in test suites):** `buck run tine//tools:check`
- **Unit tests:** `buck test tine//...` (one suite: `buck test tine//image:test`). Test targets are
  not build artifacts, so `buck build` does not run them and there is no cached verdict: every
  `buck test` reruns the suites it selects.
- **Auto-format + auto-fix:** `buck run tine//tools:fmt`
- **Refresh the catalog lock:** `buck run tine//tools:refresh-catalog`;
  `buck run tine//tools:verify-catalog` asserts the committed lock matches.
- **Full CI pipeline:** `tools/ci.sh` runs every check plus the example image builds and VM boot
  smokes as one fail-fast command.
- **rpm importer:** `buck run tine//tools:importer -- <verb>` runs the package import/update tool
  in the dev box with the host's git identity and network; see [importer.md](docs/importer.md).
- **BuildRequires cycle analysis:** `buck run tine//tools:scc -- <branch label>`; background in
  [self-host-approaches.md](docs/self-host-approaches.md).
- **Inspect a failed action's stderr:** `tools/buck log what-ran --failed --show-std-err` prints the
  full stderr of the actions that failed in the last build — buck truncates it in the build output,
  but this recovers it in full (no need to re-run or redirect anything).

## Starlark formatting

`starlark_fmt` (pinned alongside buck2, whose fork publishes it) owns the layout of every `.bzl` and
`BUCK` file; it wraps at 160 columns and reflows anything a magic trailing comma does not pin open.

- To keep a signature or call broken across lines, leave a trailing comma after its last argument.
- It sorts dict keys unconditionally. When a dict's key order is a contract (`disk.bzl:partition`
  feeds partition UUIDs), mark it `# @unsorted-dict-items`.
- It drops load symbols it cannot see used; annotate type-only imports `# @unused`.

## Architecture

See [the design plan](docs/design.md).

## Workspace compatibility

- Use cell-relative `tine//...` labels for same-cell targets, never `root//...`; this cell is named `tine`
  both standalone and in a consuming project.
- Never add a `.buckroot` here. Buck takes the *furthest* ancestor `.buckconfig` and `.buckroot` stops that
  search, so one would keep this checkout out of a Tine workspace, where it supplies the `tine` cell.
- The flip side: a stale `.buckconfig` in any parent directory captures the project, and `//...` then
  resolves to zero targets while still exiting 0. If a command mysteriously finds nothing, check
  `tools/buck root --kind project` before anything else.

## Python guidelines

- Don't use `TypeVar`; use the 3.12+ generics syntax.
- Use context managers (`ExitStack`/`AsyncExitStack` where needed) for anything that needs cleanup.
- Never use `from __future__ import annotations`.
- Always use `Self` to refer to a class's own type, not string types.
- Always use `pathlib.Path` (not `os.path`/string paths); prefer `Path` methods over `os.*`.
- Keep the check command green (see Commands).

### Paths in action scripts

Buck hands drivers project-relative paths and runs them at the project root. Keep them that way by
default; making a path absolute is a decision that needs a reason.

- **Relative (the default).** Anything read, written, or passed to a subprocess that inherits the
  action's cwd. No conversion, no filesystem access.
- **`.absolute()`.** Only when something downstream changes the frame of reference: a chroot, a
  subprocess given its own `cwd=`, a mount option the kernel resolves itself, a path written into a
  spec another process consumes, or a library that documents needing absolute paths (libdnf5 does).
  Say which in a comment.
- **`.resolve()`.** Only to inspect what a link points at, as `rootfs.py` does for overlay lowers
  (`lstat`/`getxattr` must see the real directory) and `sandbox.py` for the tools tree's usr-merge
  symlinks. It follows symlinks, and `[buck2] cell_execution_paths = canonical_v1` presents sources
  through a symlink farm, so resolving a source path collapses it back to the physical checkout and
  undoes the cache normalization.

## Commit guidelines

- Always sign off commits.
- Never add AI attribution — no `Co-Authored-By: Claude`, no "Generated with Claude Code" trailer,
  no mention of an AI/agent anywhere in commit messages or PR bodies.
