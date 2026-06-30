# AGENTS.md

## General

- **Never touch `buck-out/` directly** — no `rm`, `mv`, edits, or reads-for-mutation. Force a
  rebuild with buck (`buck clean`, then rebuild the target); to inspect an output, get its path
  from `buck build --show-output`/`--out` and read it read-only.
- Line break documents and plans at 109 columns.
- Keep comments concise; say each thing once. Comments explain *why*, not *what*.

## Layout

The `tine` cell: reusable, format-neutral machinery — rules in `defs/rules`, the rpm format
plugin + drivers in `distribution/rpm`, the vendored sandbox in `distribution`, dev tooling here.
Carries no distribution *data*. The repo root consumes the cell: it supplies the data
(`root//distribution`) and package targets (`root//distribution/packages`) plus toolchain wiring.

## Commands

Tools are pinned under `tools/` and put on `$PATH` by a `SessionStart` hook
`.claude/settings.json`. Invoke them by bare name. Run `just` recipes as `just <recipe>`.

- **buck (whole project):** `buck build //...`.
- **Check (format, lint, type-check):** `just check`
- **Auto-format + auto-fix:** `just fmt`
- **Refresh the catalog lock:** `just refresh-catalog`; `just verify-catalog` asserts the committed lock matches.

## Architecture

See [the design plan](docs/design.md).

## Python guidelines

- Don't use `TypeVar`; use the 3.12+ generics syntax.
- Use context managers (`ExitStack`/`AsyncExitStack` where needed) for anything that needs cleanup.
- Never use `from __future__ import annotations`.
- Always use `Self` to refer to a class's own type, not string types.
- Always use `pathlib.Path` (not `os.path`/string paths); prefer `Path` methods over `os.*`.
- Keep `just check` green.

## Commit guidelines

- Always sign off commits.
- Never add AI attribution — no `Co-Authored-By: Claude`, no "Generated with Claude Code" trailer,
  no mention of an AI/agent anywhere in commit messages or PR bodies.
