# AGENTS.md

## General

- **Never touch `buck-out/` directly** — no `rm`, `mv`, edits, or reads-for-mutation. Force a
  rebuild with buck (`buck clean`, then rebuild the target); to inspect an output, get its path
  from `buck build --show-output`/`--out` and read it read-only.
- Line break documents and plans at 109 columns.
- Keep comments concise; say each thing once. Comments explain *why*, not *what*.

## Layout

The `tine` cell contains reusable machinery organized by subsystem. Starlark rules and their
drivers live together in `engine`, `package`, `package_system`, `image`, `image_format`, `rootfs`,
and `archive`. Vendored code lives in `vendor`. The default catalog remains in `catalog`; consumers
may repoint that cell. Package sources and targets live in the root repository under `distribution`.

## Commands

The pinned `buck` lives in `tools/`,; invoke it by path. Everything else (python3, ruff, ty, buildifier)
is pinned in `tools/BUCK` and fetched by buck itself; the dev commands are `buck run` targets.

- **buck (whole project):** `buck build //...`.
- **Check (format, lint, type-check):** `buck run tine//tools:check`
- **Auto-format + auto-fix:** `buck run tine//tools:fmt`
- **Refresh the catalog lock:** `buck run tine//tools:refresh-catalog`;
  `buck run tine//tools:verify-catalog` asserts the committed lock matches.
- **BuildRequires cycle analysis:** `buck run tine//tools:scc -- <branch label>`; background in
  [self-host-approaches.md](docs/self-host-approaches.md).
- **Inspect a failed action's stderr:** `tools/buck log what-ran --failed --show-std-err` prints the
  full stderr of the actions that failed in the last build — buck truncates it in the build output,
  but this recovers it in full (no need to re-run or redirect anything).

## Architecture

See [the design plan](docs/design.md).

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
