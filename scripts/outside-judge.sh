#!/bin/bash
# Usage: outside-judge.sh [--host codex|claude] CONTRACT HANDOFF [REPOSITORY]
# The Python helper selects the other provider and never retries or falls back.
set -u
SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
if [ "$SCRIPT_DIR" = "${BASH_SOURCE[0]}" ]; then SCRIPT_DIR=.; fi
HERE="$(cd -- "$SCRIPT_DIR" && pwd)" || exit 3
exec python3 "$HERE/outside_judge.py" "$@"
