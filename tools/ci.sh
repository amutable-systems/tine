#!/bin/bash
# The full CI pipeline
set -euo pipefail
cd "$(dirname "$0")/.."
buck=tools/buck

# GitHub folds each step into a log group; an interactive terminal gets a bold banner; anything else
# (piped to a file, another CI) gets a bare `=== label ===` line with no escape sequences.
if [ "${GITHUB_ACTIONS:-}" = true ]; then mode=github
elif [ -t 1 ]; then
    mode=tty
    bold=$'\033[1m'
    dim=$'\033[2m'
    reset=$'\033[0m'
    # The rule's accent is an ANSI palette colour (not a fixed RGB), so the terminal keeps it legible
    # on its own light/dark theme. Not bold: that renders bright and washes out on light backgrounds.
    accent=''
    [ -z "${NO_COLOR:-}" ] && accent=$'\033[36m'
else mode=plain
fi

# A full-width ━ rule in the accent colour.
hrule() {
    local width fill
    width=$(tput cols 2>/dev/null || echo 72)
    printf -v fill '%*s' "$width" ''
    printf '%s%s%s\n' "$accent" "${fill// /━}" "$reset"
}

# A step banner: blank line; a rule opening `━━━ label ` with the label default-fg bold and the dashes
# accented; the dim command for copy-paste; then a closing rule.
banner() {
    local label=$1 width tail used
    shift
    width=$(tput cols 2>/dev/null || echo 72)
    used=$((4 + ${#label} + 1))  # "━━━ " + label + trailing space
    printf -v tail '%*s' "$((width > used ? width - used : 0))" ''
    printf '\n%s━━━ %s%s%s %s%s%s\n' "$accent" "$reset$bold" "$label" "$reset" "$accent" "${tail// /━}" "$reset"
    printf '%s%s%s\n' "$dim" "$*" "$reset"
    hrule
}

# group LABEL -- CMD...: run one labelled step, fail-fast. On GitHub, close the group and surface an
# ::error:: after it (a failure buried inside a collapsed group is invisible).
group() {
    local label=$1
    shift 2
    case $mode in
        github) printf '::group::%s\n' "$label" ;;
        tty) banner "$label" "$@" ;;
        plain) printf '\n=== %s ===\n' "$label" ;;
    esac
    local rc=0
    "$@" || rc=$?
    if [ "$mode" = github ]; then
        printf '::endgroup::\n'
        [ "$rc" -eq 0 ] || printf '::error::ci step %s failed\n' "$label"
    fi
    return "$rc"
}

# Build the from-source Rust image's SBOM to stdout and assert the crate graph reached it. Captured on
# its own line so `set -e` still catches a buck failure before grep runs.
rust_sbom() {
    local sbom
    sbom=$("$buck" build 'tine//examples/image-rust-project:demo[sbom][cyclonedx]' --out -)
    # our own rust packages
    grep -q 'pkg:cargo/hello@0.1.0' <<< "$sbom"
    grep -q 'pkg:cargo/nodeps@0.1.0' <<< "$sbom"
    # hello's dependency from crates.io
    grep -q 'pkg:cargo/serde_json@' <<< "$sbom"
    # hello's dependency from git
    grep -q 'pkg:cargo/anyhow@' <<< "$sbom"
}

# First invocation fetches buck's pinned tools and builds the shared engine; kept its own group so
# bootstrap time stays visible.
group engine           -- "$buck" build tine//catalog:fedora.rawhide.engine
group check            -- "$buck" run tine//tools:check
# CentOS Stream mirrors are intentionally unpinned and drift, so only the rawhide engine is verifiable.
group verify-catalog   -- "$buck" run tine//tools:verify-catalog -- --engine fedora.rawhide.engine
group box              -- "$buck" build tine//examples/box:box
group boot-demo-image  -- "$buck" build tine//examples/image:boot-demo
group boot-demo-smoke  -- "$buck" run tine//examples/image:boot-demo-vm-smoke
group rust-sbom        -- rust_sbom
group secureboot-image -- "$buck" build tine//examples/image-secureboot:image
group secureboot-smoke -- "$buck" run tine//examples/image-secureboot:vm-smoke
