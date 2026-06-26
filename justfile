# Command runner for the project's in-tree Python/Starlark tooling. Lives in the
# tine cell (run it from here: `cd tine && ./tools/just <recipe>`), but its recipes
# drive the *whole* buck2 project: `working-directory := '..'` runs them at the
# repo root, so buck2 targets, the prelude/buck-out paths, and the `find` sweeps
# below are repo-root-relative (covering both the tine cell and the root consumer).
# All tools are pinned via DotSlash under tine/tools/ — no host install needed.
set working-directory := '..'

ruff       := "./tine/tools/ruff"
ty         := "./tine/tools/ty"
buck2      := "./tine/tools/buck2"
buildifier := "./tine/tools/buildifier"

# Python source we own — all under the tine cell (the vendored mkosi/sandbox.py is
# excluded in pyproject.toml). Scoped to distribution/ so the sweep never descends
# the `tine/.engine` symlink (which points at the engine's own python tree).
PY_DIRS := "./tine/distribution"

# The engine whose root the lock generator self-hosts in. The distributions are
# declared in the catalog cell (default tine/catalog), so the engine root and buckify
# targets live at `catalog//:`.
ENGINE := "fedora44"

# Format, lint, type-check (Python via ruff/ty, Starlark via buildifier + buck2).
# buildifier formats .bzl + BUCK (generated.bzl excluded — buckify owns its
# layout). Two gitignored repo-root symlinks back the type checkers: `.engine` →
# the built engine root (ty resolves libdnf5/createrepo_c from the pinned env the
# drivers run in, not the host — passed via `--python`); `prelude` → the bundled
# prelude in buck-out (so the Starlark typechecker resolves `@prelude//`). Building
# the engine root also materializes the prelude. ty needs `--project tine` because
# it discovers config from the cwd (the repo root here), not from the checked path.
check:
	{{ruff}} format --check {{PY_DIRS}}
	{{ruff}} check {{PY_DIRS}}
	ln -sfn $({{buck2}} build catalog//:engine.{{ENGINE}}.root --show-output 2>/dev/null | awk '{print $2}') .engine
	{{ty}} check --project tine --python .engine/usr {{PY_DIRS}}
	{{buildifier}} -mode=check $(find . \( -name '*.bzl' -o -name BUCK \) -not -path './buck-out/*' -not -path './prelude/*' -not -name generated.bzl)
	{{buck2}} -v 0 starlark lint $(find . -name '*.bzl' -not -path './buck-out/*' -not -path './prelude/*')
	ln -sfn buck-out/v2/external_cells/bundled/prelude prelude
	{{buck2}} -v 0 starlark typecheck $(find . -name '*.bzl' -not -path './buck-out/*' -not -path './prelude/*')

# Auto-format and auto-fix lints (Python via ruff, Starlark via buildifier).
fmt:
	{{ruff}} format {{PY_DIRS}}
	{{ruff}} check --fix {{PY_DIRS}}
	{{buildifier}} $(find . \( -name '*.bzl' -o -name BUCK \) -not -path './buck-out/*' -not -path './prelude/*' -not -name generated.bzl)

# Regenerate a catalog: the host orchestrator (//distribution:buckify) drives each
# distribution's resolver in *its own* engine (one fragment per distribution), then amalgamates
# the fragments into generated.bzl. Resolution uses the pinned libdnf5, not the
# host. Defaults to the cell's own catalog; a consumer regenerates their own with
# `just refresh-catalog my/catalog` (run against tine's engine while bootstrapping, then
# their own once `[cells] catalog` points at it). --buck2 is passed so the host tool
# can nest buck2 (a python child can't locate buck2 the way buck2 locates itself).
refresh-catalog catalog_dir="tine/catalog":
	{{buck2}} run tine//distribution:buckify -- --catalog-dir {{catalog_dir}} --buck2 {{buck2}}

# CI: the committed default catalog (fragments + amalgamation) must equal what the
# pinned resolvers produce.
verify-catalog: refresh-catalog
	git diff --exit-code -- 'tine/catalog/*.json' 'tine/catalog/generated.bzl'
