# Treat `#` lines inside recipes as real comments — don't echo them or hand them to the shell.
set ignore-comments := true

# Put the pinned tools (tools/) first on PATH so recipes invoke them by bare name. Built from
# justfile_directory() so it's absolute — holds from any recipe cwd, embedded or standalone.
export PATH := (justfile_directory() / "tools") + ":" + env_var("PATH")

PY_DIRS := "./distribution"

# FIXME: type check python in every engine? Some python dependencies will only be
# available in certain engines.
ENGINE := "fedora44"

check:
	ruff format --check {{PY_DIRS}}
	ruff check {{PY_DIRS}}
	# Build the engine root (ty's pinned third-party env) and point ty straight at it — no symlink.
	@echo '{{ style("bold", "ty") }}'
	@ty check --python $(buck build catalog//:{{ENGINE}}.engine --show-full-simple-output --console none -v 0)/usr {{PY_DIRS}}
	# The `@`-prefixed lines run without just echoing them — the inline $(find …) makes the echo
	# unreadable — so an `echo` label stands in. -lint=warn runs buildifier's linter (default set);
	# the two `function-docstring` subtractions drop the mandate to document every arg + return
	# value (too verbose for our prose docstrings).
	@echo '{{ style("bold", "buildifier") }}'
	@buildifier -mode=check -lint=warn -warnings=-function-docstring-args,-function-docstring-return $(find . \( -name '*.bzl' -o -name BUCK \) -not -path './buck-out/*' -not -path './prelude/*')
	@echo '{{ style("bold", "starlark lint") }}'
	@buck -v 0 starlark lint --console none $(find . -name '*.bzl' -not -path './buck-out/*' -not -path './prelude/*')
	# The Starlark typechecker resolves @prelude// through the prelude cell's on-disk mount. Create
	# the symlink (discovered mount → bundled prelude in buck-out) just for the check, then remove it
	# via a trap so it never lingers. 2>/dev/null drops the per-file event log; errors go to stdout.
	@echo '{{ style("bold", "starlark typecheck") }}'
	@mount="$(buck audit cell prelude --paths-only 2>/dev/null)"; \
		ln -sfn "$(buck root --kind project)/buck-out/v2/external_cells/bundled/prelude" "$mount"; \
		trap 'rm -f "$mount"' EXIT; \
		buck -v 0 starlark typecheck $(find . -name '*.bzl' -not -path './buck-out/*' -not -path './prelude/*') 2>/dev/null

# Auto-format and auto-fix lints (Python via ruff, Starlark via buildifier).
fmt:
	ruff format {{PY_DIRS}}
	ruff check --fix {{PY_DIRS}}
	@echo '{{ style("bold", "buildifier") }}'
	@buildifier $(find . \( -name '*.bzl' -o -name BUCK \) -not -path './buck-out/*' -not -path './prelude/*')

# Regenerate the catalog. buckify runs its nested `buck run`s from the project root itself and
# defaults to the active catalog cell, so a consumer regenerates their own lock with no
# arguments. The pinned buck is on PATH (exported above), inherited by the nested children.
refresh-catalog:
	@echo '{{ style("bold", "buckify") }}'
	@buck -v 0 run tine//distribution:buckify --console none -- --buck buck

# CI: the committed catalog (per-repository + per-engine fragments) must equal what the
# pinned resolvers produce.
verify-catalog:
	@echo '{{ style("bold", "buckify") }}'
	@buck -v 0 run tine//distribution:buckify --console none -- --verify --buck buck
