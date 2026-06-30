ruff       := "./tools/ruff"
ty         := "./tools/ty"
buck      := "./tools/buck"
buildifier := "./tools/buildifier"

# Absolute path to this cell (the justfile's dir), for recipes that must run from the buck
# project root — a parent when tine is embedded as a cell, or tine itself when standalone.
tine := justfile_directory()

PY_DIRS := "./distribution"

# FIXME: type check python in every engine? Some python dependencies will only be
# available in certain engines.
ENGINE := "fedora44"

check:
	{{ruff}} format --check {{PY_DIRS}}
	{{ruff}} check {{PY_DIRS}}
	# Build the engine root (ty's pinned third-party env) and point ty straight at it — no symlink.
	{{ty}} check --python $({{buck}} build catalog//:engine.{{ENGINE}}.root --show-full-simple-output --console none -v 0)/usr {{PY_DIRS}}
	{{buildifier}} -mode=check $(find . \( -name '*.bzl' -o -name BUCK \) -not -path './buck-out/*' -not -path './prelude/*' -not -name generated.bzl)
	{{buck}} -v 0 starlark lint $(find . -name '*.bzl' -not -path './buck-out/*' -not -path './prelude/*')
	# The Starlark typechecker resolves @prelude// through the prelude cell's on-disk mount. Create
	# the symlink (discovered mount → bundled prelude in buck-out) just for the check, then remove it
	# via a trap so it never lingers. 2>/dev/null drops the per-file event log; errors go to stdout.
	mount="$({{buck}} audit cell prelude --paths-only 2>/dev/null)"; \
		ln -sfn "$({{buck}} root --kind project)/buck-out/v2/external_cells/bundled/prelude" "$mount"; \
		trap 'rm -f "$mount"' EXIT; \
		{{buck}} -v 0 starlark typecheck $(find . -name '*.bzl' -not -path './buck-out/*' -not -path './prelude/*') 2>/dev/null

# Auto-format and auto-fix lints (Python via ruff, Starlark via buildifier).
fmt:
	{{ruff}} format {{PY_DIRS}}
	{{ruff}} check --fix {{PY_DIRS}}
	{{buildifier}} $(find . \( -name '*.bzl' -o -name BUCK \) -not -path './buck-out/*' -not -path './prelude/*' -not -name generated.bzl)

# Regenerate a catalog. Runs from the buck project root: `buck run` launches buckify with the
# invocation cwd, and buckify nests further `buck run`s whose python wrappers resolve against the
# project root — so cd there first (a no-op when tine is the root). Absolute tine paths hold
# whether embedded or standalone; a consumer regenerates their own with `refresh-catalog <dir>`.
refresh-catalog catalog_dir=(justfile_directory() / "catalog"):
	cd "$({{tine}}/tools/buck root --kind project)" && \
		{{tine}}/tools/buck -v 0 run tine//distribution:buckify --console none -- --catalog-dir "{{catalog_dir}}" --buck {{tine}}/tools/buck

# CI: the committed default catalog (fragments + amalgamation) must equal what the
# pinned resolvers produce.
verify-catalog: refresh-catalog
	git diff --exit-code -- 'catalog/*.json' 'catalog/generated.bzl'
