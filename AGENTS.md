# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## General

- **Never touch `buck-out/` directly** — no `rm`, `mv`, edits, or reads-for-mutation.
  It's buck2's managed state; hand-editing it desyncs the daemon's materialization
  tracking and corrupts the build (actions then fail on missing intermediate outputs).
  To force a rebuild use buck2 itself (`buck2 clean`, rebuild the target); to inspect an
  output, get its path from `buck2 build --show-output`/`--out` and read it read-only.
- Always line break documents and plans at 109 columns to keep them readable on
  small panes.
- Keep comments concise and say each thing once. Don't restate what the code (or a
  nearby docstring/comment) already makes clear, and don't repeat an explanation
  across files — put it in one canonical place and let the others stay terse.
  Comments earn their keep by explaining *why*, not narrating *what*.

## Layout

This directory is the `tine` cell: the reusable, format-neutral machinery (rules
in `defs/rules`, the rpm format plugin + drivers in `distribution/rpm`, the
vendored sandbox in `distribution`, and all dev tooling here). It carries no
distribution *data*. The repo root is a thin **consumer** of the cell: it supplies
the data (`root//distribution`: manifest, lock, engine roots, repos, flavors) and
the package targets (`root//distribution/packages`), plus the toolchain wiring.

## Commands

All dev tools are pinned via DotSlash under `tools/` — no host install needed. Run
them from this directory (`cd tine`); the `just` recipes set their working
directory to the repo root, so they drive the whole project (cell + consumer).

- **buck2 (whole project):** from the repo root, `./tine/tools/buck build //...`;
  the cell's own targets are `tine//...`.
- **Check (format, lint, type-check):** `./tools/just check`
- **Auto-format + auto-fix:** `./tools/just fmt`
- **Refresh the catalog lock:** `./tools/just refresh-catalog` — the host orchestrator
  drives each distribution's resolver in its own engine (pinned libdnf5, not the
  host) and amalgamates the fragments. `./tools/just verify-catalog` asserts the
  committed lock matches (CI fixpoint check).
- **Integration test (run after any change to the rules, drivers, sandbox, or catalog):**
  from the repo root, `./tine/tools/buck build root//distribution/packages/zlib:`. This
  builds the zlib rpms for every distribution — `:zlib` (fedora44, self-hosted engine) and
  `:zlib.centos10` (centos10, cross-built in the shared Fedora engine) — exercising the whole
  path end-to-end: engine root → buildroot assembly → `rpmbuild` → the subpackage fidelity
  gate. If it's green for both distros, the core machinery still works.

## Architecture

A buck2 project that rebuilds a core subset of Fedora: every rpm is a buck2
target, built by `rpmbuild` under a vendored mkosi-sandbox, scheduled in
dependency order with full caching. See [the design plan](docs/design.md) for the full picture.
Python tooling targets 3.14+ (the bootstrap needs stdlib `compression.zstd`).

## Python guidelines

- Don't use `TypeVar`; use the 3.12+ generics syntax.
- Use context managers (`ExitStack`/`AsyncExitStack` where needed) for anything
  that needs cleanup.
- Never use `from __future__ import annotations`.
- Always use `Self` to refer to a class's own type, not string types.
- Always use `pathlib.Path` for filesystem paths (not `os.path`/string paths);
  prefer `Path` methods (`.read_bytes()`, `.iterdir()`, `/` joins) over `os.*`.
- All Python is fully typed, formatted, and linted (`./tools/just check` is green).

## Commit guidelines

- Always sign off commits.
- Never add AI attribution — no `Co-Authored-By: Claude`, no "Generated with Claude
  Code" trailer, no mention of an AI/agent anywhere in commit messages or PR bodies.
