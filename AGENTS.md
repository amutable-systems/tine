# AGENTS.md

## General

- **Never touch `buck-out/` directly** — no `rm`, `mv`, edits, or reads-for-mutation. Force a
  rebuild with buck (`buck clean`, then rebuild the target); to inspect an output, get its path
  from `buck build --show-output`/`--out` and read it read-only.
- **Never `buck build | tail`** — redirect full output to a log file, show the path so that the user can
  follow along, and check exit separately. Inspect failed actions with
  `tine buck log what-ran --failed --show-std-err`.
- Line break documents and plans at 109 columns.
- Keep comments concise; say each thing once. Comments explain *why*, not *what*.

## Layout

The `tine` cell contains reusable machinery organized by subsystem. Starlark rules and their
drivers live together in `box`, `package`, `package_system`, `cargo`, `go`, `image`, `image_format`,
`rootfs`, and `archive`; `python` owns their shared driver rules. Vendored code lives in `vendor`, and
`bin` holds the `tine` command. The default catalog is `tine//catalog`; consumers may instead declare a
project-specific `//catalog` package.
`examples` holds the demo images that CI builds and boot-tests, plus a dev box. Package sources and
targets live in the OS.git root that consumes this cell, under `packages/` (built as `//packages/...`).

Unit tests sit beside the driver they exercise as `<driver>_test.py`, run by a `box_python_test`
target in the same package (`tine//box:defs.bzl`), which only `buck test` runs. Cross-package
sources reach a suite through `deps` on a `tine_python_library`, never `export_file`. An
assertion about what a build produced is a `box_sh_test` beside the target that produced it: a
script taking artifacts as `$(location)` arguments, run in a box so it reaches pinned tools
rather than the host'"'"'s. `tests` holds what those share, like the VM boot smoke driver.

Python drivers use `tine_python_library` and `tine_python_binary`, macros that declare the prelude's
bootstrap target and, beside it, a `<name>-ty` check over that target's own sources in a flat tree of their
transitive runtime. `lint` discovers those checks by label; never declare a bootstrap target directly or
add its ty check by hand. A dep's sources are in the tree to resolve imports and are checked by their own
target, so each file is checked once, wherever it is declared (`typecheck = False` declares no check at
all, for vendored sources that follow their own rules). Every check names the environment its imports
resolve against, so none can pass on what a developer happens to have installed: the pinned interpreter by
default, or the `boxes` a driver reaching for third-party modules lists, one check per box. A box Python
test checks its own sources against the box that runs them.

## Commands

`bin/tine` is the entry point, a command of its own, and `mise.toml` puts `./bin` on `PATH` so it is
spelled `tine`. **Every `buck` below means `tine buck`**: nothing else on this machine is the Buck2 this
project pins. `tine buck <arguments>` fetches that Buck2 (pinned in `tools/tools.json`) and execs it with
everything after `buck` untouched. `tine init [<path>]` writes a project's configuration for a checkout of
this cell in it, this command's own by default, by copying this cell's `.buckconfig` and repointing the
cells; `tine cell override <cell> <path>` builds a cell from a checkout on this machine,
`tine cell revert <cell>` goes back to the pinned one, and `tine cell list` prints what is declared.

An override follows the checkout's `HEAD`, or the revision `--commit` names. Those declarations live in
the block `tine` owns in the gitignored `.buckconfig.local`, which is also what Buck reads, so there is
nothing to keep in step with it. One that stops resolving fails every command that runs Buck rather than
falling back to what `.buckconfig` pins, and overriding the `tine` cell hands the command over to that
checkout's `bin/tine` as well. Every pinned tool is declared in `tools/tools.json`. The dev commands are
`buck run` targets.

It also injects configuration derived from the checkout and from those overrides (a block it owns in
`.buckconfig.local`; see README) and serves its own shell completion, Buck2's own hanging off
`tine buck`. A tool Buck itself runs finds that same binary in `$BUCK2_BINARY` and can nest a command
with it, which skips the refresh rather than redoing it underneath the build that started it.

- **buck (whole project):** `buck build tine//...` — builds everything but runs no tests; see below.
- **Lint (format, lint, type-check, whole-graph analysis):** `buck run tine//tools:lint`
- **Check (lint plus the hooked-in test suites):** `buck run tine//tools:check`
- **Analyze every target without building one:** `buck bxl tine//tools/graph.bxl:analyze`. Catches a rule
  that fails during analysis in a target nothing happens to build; `--` `--pattern` scopes it elsewhere.
- **Unit tests:** `buck test tine//...` (one suite: `buck test tine//image:test`). Anything needing an
  example image built is a test too and carries the `image` label, the boot smokes additionally `vm`;
  `--exclude image` is the seconds-long half, which `check` runs, and `--include image` is the rest,
  which CI runs as its own step. Test targets are
  not build artifacts, so `buck build` does not run them and there is no cached verdict: every
  `buck test` reruns the suites it selects.
- **Auto-format + auto-fix:** `buck run tine//tools:fmt`
- **Refresh the catalog lock:** `buck run tine//tools:refresh-catalog`;
  `buck run tine//tools:verify-catalog` asserts the committed lock matches.
- **Full CI pipeline:** `tools/ci.sh` runs the graph analysis, both box bootstraps, `check`,
  `verify-catalog`, a build of everything, the `image`-labelled tests, and the PKCS#11 signing
  harness, as one fail-fast command.
- **rpm importer:** `buck run tine//tools:importer -- <verb>` runs the package import/update tool
  in the dev box with the host's git identity and network; see [importer.md](docs/importer.md).
- **BuildRequires cycle analysis:** `buck run tine//tools:scc -- <branch label>`; background in
  [self-host-approaches.md](docs/self-host-approaches.md).
- **Inspect a failed action's stderr:** `tine buck log what-ran --failed --show-std-err` prints the
  full stderr of the actions that failed in the last build — buck truncates it in the build output,
  but this recovers it in full (no need to re-run or redirect anything).

## Starlark formatting

`starlark_fmt` (pinned alongside buck2, whose fork publishes it) owns the layout of every `.bzl` and
`BUCK` file; it wraps at 160 columns and reflows anything a magic trailing comma does not pin open.

- To keep a signature or call broken across lines, leave a trailing comma after its last argument.
- It sorts dict keys unconditionally. When a dict's key order is a contract (`disk.bzl:partition`
  feeds partition UUIDs), mark it `# @unsorted-dict-items`.
- It drops load symbols it cannot see used; annotate type-only imports `# @unused`.

## Declaration macros

A macro that hands its arguments straight to a rule forwards them as `**kwargs`. An argument the macro
*reads* is a typed parameter instead, never a `kwargs` lookup: the signature is where a caller finds out
that an argument exists. For the same reason a helper takes those values as parameters rather than the
whole `kwargs` dict. Buck reads `None` as "use the attribute's default", so such a parameter defaults to
`None` rather than restating the rule's default, and one a caller may reach with a `select()` is annotated
`X | Select`.

## Rule implementations

`ctx.attrs` belongs to the rule implementation that owns those attributes, and to nothing else. A `_impl`
reads whatever it likes from it; every other function takes what it needs as typed parameters, for the same
reason a macro's helpers do. Otherwise a shared helper silently requires an attribute of a name and type
its signature never mentions, and the next rule to call it fails at analysis rather than at the call.

## Architecture

See [the design plan](docs/design.md).

## Workspace compatibility

- Use cell-relative `tine//...` labels for same-cell targets, never `root//...`; this cell is named `tine`
  both standalone and in a consuming project.
- Never add a `.buckroot` here. Buck takes the *furthest* ancestor `.buckconfig` and `.buckroot` stops that
  search, so one would keep this checkout out of a Tine workspace, where it supplies the `tine` cell.
- The flip side: a stale `.buckconfig` in any parent directory captures the project, and `//...` then
  resolves to zero targets while still exiting 0. If a command mysteriously finds nothing, check
  `tine buck root --kind project` before anything else.

## Python guidelines

- Don't use `TypeVar`; use the 3.12+ generics syntax.
- Use context managers (`ExitStack`/`AsyncExitStack` where needed) for anything that needs cleanup.
- Never use `from __future__ import annotations`.
- Always use `Self` to refer to a class's own type, not string types.
- Always use `pathlib.Path` (not `os.path`/string paths); prefer `Path` methods over `os.*`.
- Keep the check command green (see Commands).

### Paths in action scripts

Buck hands drivers project-relative paths and runs them at the project root, which a build sandbox mounts
at `/tine/project`. Keep them that way by default; making a path absolute is a decision that needs a reason.

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

### Keep the message short

A commit message is a subject plus, at most, one short paragraph. Most commits need two or three
sentences. Two paragraphs is the ceiling, and only a genuinely subtle change earns the second. Wrap at
80 columns.

- **Subject:** under 60 characters, `area: what changed`, imperative mood. It is a title, not a summary.
- **Body:** what was wrong or missing, then what this does about it. Nothing else. A one-line body is
  fine; drop the body only when the change is trivial.
- **Write it like you would say it to a colleague at their desk.** Short words, short sentences, plain
  verbs. Read it out loud; if it sounds like a press release or a design document, rewrite it.
- **Never restate the diff.** No file lists, no function-by-function walkthrough, no "this commit adds".
  The reader already has the diff. What they do not have is the reason.
- **Cut the padding.** No scene-setting sentence before the point, no sentence recapping it at the end,
  no "in order to", "note that", "it is worth mentioning". Delete every sentence that is not the
  problem or the fix.
- **No bullet lists** unless the commit really does several independent things, which usually means it
  should have been several commits.

A good body reads like this:

```
The daemon kept a stale mount digest, so a second build reused a namespace
that no longer matched. Compare the digest on startup and kill the daemon
when it differs.
```

Not like this:

```
This commit introduces a comprehensive improvement to the daemon lifecycle
management logic. Previously, the implementation did not take into account
the possibility that the mount digest recorded at startup could diverge
from the digest of the currently requested mount configuration, which in
certain circumstances could lead to a scenario where a build would be
executed inside a namespace that was no longer representative of ...
```
