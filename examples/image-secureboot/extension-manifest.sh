#!/bin/bash
# The listing published beside an extension DDI describes what the extension ships rather than the
# /usr it merges onto: zsh, which this extension installs, and none of the base image whose layers
# it sits on. What it carries outside /usr and /opt is manifest-scope.sh's half of the question.
set -euo pipefail

manifest=$1
status=0
if ! grep -q '"name":"usr/bin/zsh"' "$manifest"; then
    echo "$(basename "$manifest"): the extension's own zsh is not listed" >&2
    status=1
fi
if grep -q '"name":"usr/lib/systemd/systemd"' "$manifest"; then
    echo "$(basename "$manifest"): the base image is listed, so this is not the delta" >&2
    status=1
fi
exit "$status"
