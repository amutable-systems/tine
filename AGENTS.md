# AGENTS.md

## General

- **Never touch `buck-out/` directly**: no `rm`, `mv`, or edits. To inspect an output, get its path from
  `buck build --show-output`/`--out` and read it read-only.
- **Never `buck build | tail`**: redirect the full output to a log file, print the path so the user can
  follow along, and check the exit status separately. `tine buck log what-ran --failed --show-std-err`
  prints the full stderr of the actions that failed in the last build, which the build output truncates.
- Keep `buck run tine//tools:check` green.
- Line break documents and plans at 109 columns.
- Comments explain *why*, not *what*; say each thing once.
- Do not write user-facing documentation. At most, add a TODO comment where documentation should be
  written by a human.

## Layout

Cell layout and architecture: [the design plan](docs/design.md).

- A unit test is `<driver>_test.py` beside its driver, run by a `box_python_test` in the same package.
- An assertion about what a build produced is a `box_sh_test` beside the target that produced it, taking
  artifacts as `$(location)` arguments so it runs against pinned tools rather than the host's.
- Python sources reach a suite in another package through `deps` on a `tine_python_library`, never
  `export_file`.
- Python drivers use `tine_python_library`/`tine_python_binary`. Never declare the prelude's bootstrap
  target or a `-ty` check by hand; the macros do both.
- A driver importing third-party modules lists the `boxes` its check resolves against, one check per box.
  `typecheck = False` is for vendored sources only.

## Commands

**Every `buck` below means `tine buck`**: nothing else on this machine is the Buck2 this project pins.
`mise.toml` puts `./bin` and `./tools` on `PATH`; every pinned tool is declared in `tools/tools.json`.
`tine buck` runs Buck with the mounts declared by `tine mount` in `.buck/tine-mounts.toml`.

- **Build everything:** `buck build tine//...`, which runs no tests.
- **Lint (format, lint, type-check, whole-graph analysis):** `buck run tine//tools:lint`
- **Check (lint plus the fast test suites):** `buck run tine//tools:check`
- **Auto-format + auto-fix:** `buck run tine//tools:fmt`
- **Analyze part of the graph without building it:** `buck bxl tine//tools/graph.bxl:analyze -- --pattern
  <pattern>`; `lint` already runs it over everything.
- **Tests:** `buck test tine//...`, or one suite: `buck test tine//image:test`. Anything needing an example
  image carries the `image` label, the boot smokes additionally `vm`; `--exclude image` is the
  seconds-long half that `check` runs, `--include image` the rest, which CI runs as its own step.
- **Catalog lock:** `buck run tine//tools:refresh-catalog`; `tools:verify-catalog` asserts the committed
  lock matches.
- **Full CI pipeline:** `tools/ci.sh`, fail-fast. Run individual groups by stating their names, e.g.
  `tools/ci.sh check build`
- **rpm importer:** `buck run tine//tools:importer -- <verb>`; see [importer.md](docs/importer.md).
- **BuildRequires cycle analysis:** `buck run tine//tools:scc -- <branch label>`; background in
  [self-host-approaches.md](docs/self-host-approaches.md).

## Starlark

`starlark_fmt` owns the layout of every `.bzl` and `BUCK` file and wraps at 160 columns.

- Leave a trailing comma after the last argument to keep a signature or call broken across lines.
- It sorts dict keys unconditionally; mark a dict whose order is a contract (`disk.bzl:partition` feeds
  partition UUIDs) `# @unsorted-dict-items`.
- It drops load symbols it cannot see used; annotate type-only imports `# @unused`.

## Macros and rules

A macro forwards arguments it does not read as `**kwargs`; one it *reads* is a typed parameter, never a
`kwargs` lookup, and its helpers take those values as parameters rather than the whole dict. Such a
parameter defaults to `None`, which Buck reads as the attribute's own default, and is annotated
`X | Select` if a caller may reach it with a `select()`.

`ctx.attrs` belongs to the rule implementation that owns those attributes. `_impl` reads whatever it likes
from it; every other function takes what it needs as typed parameters.

## Workspace

- Use cell-relative `tine//...` labels for same-cell targets, never `root//...`.
- Never add a `.buckroot` here. Buck takes the *furthest* ancestor `.buckconfig`, and `.buckroot` would
  stop that search, keeping this checkout out of a workspace it supplies the `tine` cell to.
- The flip side: a stale `.buckconfig` in a parent directory captures the project, and `//...` then matches
  nothing while still exiting 0. If a command mysteriously finds nothing, check
  `tine buck root --kind project` before anything else.

## Python

- Use PEP 257 docstring style: Short single-line summary, blank line, multi-line body.
- Don't use `TypeVar`; use the 3.12+ generics syntax. Never `from __future__ import annotations`. Use
  `Self` for a class's own type, not a string type.
- Use context managers (`ExitStack`/`AsyncExitStack` where needed) for anything that needs cleanup.
- Use `pathlib.Path` and prefer its methods over `os.*`. The exceptions are `box/sandbox.py` and
  `box/isolation.py`: use string paths and `os.path` there to keep startup imports to the absolute minimum.
- Never import `.bzl` files into Python tests, including through `runpy`.

### Paths in action scripts

Buck hands drivers project-relative paths and runs them at the project root, which a build sandbox mounts
at `/tine/project`. Keep them relative; making one absolute is a decision that needs a reason.

- **`.absolute()`** only when something downstream changes the frame of reference: a chroot, a subprocess
  given its own `cwd=`, a mount option the kernel resolves itself, a path written into a spec another
  process consumes, or a library documented as needing it (libdnf5). Say which in a comment.
- **`.resolve()`** follows symlinks on top of that, so reach for it only when following them is the
  point: `rootfs.py` resolves overlay lowers before entering a chroot, `sandbox.py` walks the tools tree
  to recreate its usr-merge links rather than bind through them.

## Commits

- Commits are signed off, but the trailer is appended automatically: never add it by hand.
- Never mention an AI or agent in a commit message or PR body, and never add a `Co-Authored-By: Claude`
  or "Generated with Claude Code" trailer.
- A subject under 60 characters, `area: what changed`, imperative. Then at most one short paragraph
  wrapped at 80 columns: what was wrong or missing, then what this does about it, and nothing else.
- Write it like you would say it to a colleague at their desk. Short words, plain verbs, no bullet lists.
- Never restate the diff: the reader has it, what they lack is the reason. Cut every sentence that is not
  the problem or the fix.

A whole commit body, in full:

```
The daemon kept a stale mount digest, so a second build reused a namespace
that no longer matched. Compare the digest on startup and kill the daemon
when it differs.
```
