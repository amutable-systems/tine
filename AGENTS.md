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
drivers live together in `engine`, `package`, `package_system`, `image`, `image_format`, `rootfs`,
and `archive`. Vendored code lives in `vendor`. The default catalog is `tine//catalog`; consumers may
instead declare a project-specific `//catalog` package. `examples` holds the demo images and dev box that
CI builds and boot-tests. Package sources and targets live in the OS.git root that consumes this cell,
under `packages/` (built as `//packages/...`).

## Commands

The pinned `buck` lives in `tools/`,; invoke it by path. Everything else (python3, ruff, ty, buildifier)
is pinned in `tools/BUCK` and fetched by buck itself; the dev commands are `buck run` targets.

- **buck (whole project):** `buck build //...`.
- **Lint (format, lint, type-check):** `buck run tine//tools:lint`
- **Check (lint plus the hooked-in test suites):** `buck run tine//tools:check`
- **Unit tests alone:** `buck build tine//tests:unit-test` — a cached build action, so it only
  reruns when the tests, the tool under test, or the dev box engine change.
- **Auto-format + auto-fix:** `buck run tine//tools:fmt`
- **Refresh the catalog lock:** `buck run tine//tools:refresh-catalog`;
  `buck run tine//tools:verify-catalog` asserts the committed lock matches.
- **rpm importer:** `buck run tine//tools:importer -- <verb>` runs the package import/update tool
  in the dev box with the host's git identity and network; see [importer.md](docs/importer.md).
- **BuildRequires cycle analysis:** `buck run tine//tools:scc -- <branch label>`; background in
  [self-host-approaches.md](docs/self-host-approaches.md).
- **Inspect a failed action's stderr:** `tools/buck log what-ran --failed --show-std-err` prints the
  full stderr of the actions that failed in the last build — buck truncates it in the build output,
  but this recovers it in full (no need to re-run or redirect anything).

## Architecture

See [the design plan](docs/design.md).

## Workspace compatibility

- Use cell-relative `//...` labels for same-cell targets, never `root//...`; `root` is the parent registry
  cell in a Tine workspace.
- Registered projects must not contain `.buckroot`, because it prevents Buck from discovering the workspace.

## Python guidelines

- Don't use `TypeVar`; use the 3.12+ generics syntax.
- Use context managers (`ExitStack`/`AsyncExitStack` where needed) for anything that needs cleanup.
- Never use `from __future__ import annotations`.
- Always use `Self` to refer to a class's own type, not string types.
- Always use `pathlib.Path` (not `os.path`/string paths); prefer `Path` methods over `os.*`.
- Keep the check command green (see Commands).

## Commit guidelines

- Always sign off commits.
- Never add AI attribution — no `Co-Authored-By: Claude`, no "Generated with Claude Code" trailer,
  no mention of an AI/agent anywhere in commit messages or PR bodies.
