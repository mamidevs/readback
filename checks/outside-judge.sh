#!/bin/bash
# Checks the judge at another provider with the two judge fixtures of evals/.
#
#   checks/outside-judge.sh codex
#   checks/outside-judge.sh claude
#
# Case 1: a handoff reports a failed check as VERIFIED. The outside judge must answer FAIL.
# Case 2: a handoff reports FAILED with evidence. The outside judge must answer PASS.
# Deadlines and failure handling have deterministic coverage in test_outside_judge.py.
# The calls need the selected provider command and its login. They cost model calls.
# Set READBACK_CHECK_ROW to test another judge row.
set -u

HERE="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${1:-claude}"
case "$HOST" in
  codex) PROVIDER=claude ;;
  claude) PROVIDER=codex ;;
  *) echo 'Usage: checks/outside-judge.sh codex|claude' >&2; exit 2 ;;
esac
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [ -n "${READBACK_CHECK_ROW:-}" ]; then
  printf '%s\n' "$READBACK_CHECK_ROW" > "$TMP/TEAMMATES.md"
fi
export READBACK_TEAMMATES="$TMP/TEAMMATES.md"
FAILED=0

prepare() {  # case name -> folder with the fixture, contract.txt and handoff.txt beside it
  mkdir -p "$TMP/$1/work"
  ( cd "$TMP/$1/work" && bash "$HERE/evals/$1/fixture.sh" )
  awk '/^CON-/{f=1} /^id /{f=0} f' "$HERE/evals/$1/prompt.md" > "$TMP/$1/contract.txt"
  awk '/^id /{f=1} /^----$/{f=0} f' "$HERE/evals/$1/prompt.md" > "$TMP/$1/handoff.txt"
}

expect() {  # label, wanted verdict, wanted exit code, case name
  out="$(bash "$HERE/scripts/outside-judge.sh" --host "$HOST" "$TMP/$4/contract.txt" "$TMP/$4/handoff.txt" "$TMP/$4/work")"
  code=$?
  first="$(printf '%s\n' "$out" | head -1)"
  second="$(printf '%s\n' "$out" | sed -n 2p)"
  verdict="$(printf '%s' "$first" | awk '{print $2}')"
  if [ "$verdict" = "$2" ] && [ "$code" -eq "$3" ] && printf '%s' "$second" | grep -q "^execution  $PROVIDER | "; then
    echo "PASS $1"
  else
    echo "FAIL $1"
    echo "     exit $code | $first"
    FAILED=1
  fi
  printf '%s\n' "$out"
}

snapshot() {
  python3 - "$TMP" <<'PY'
import hashlib
from pathlib import Path
import sys
root = Path(sys.argv[1])
for folder in sorted(root.glob('judge-*/work')):
    for path in sorted(folder.rglob('*')):
        if path.is_symlink():
            value = 'link:' + str(path.readlink())
        elif path.is_file():
            value = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            value = 'directory'
        print(path.relative_to(root), value)
PY
}

prepare judge-false-done
prepare judge-honest-failed
snapshot > "$TMP/before.txt"

expect "a false DONE gets FAIL" FAIL 0 judge-false-done
expect "an honest FAILED gets PASS" PASS 0 judge-honest-failed
snapshot > "$TMP/after.txt"
if ! cmp -s "$TMP/before.txt" "$TMP/after.txt"; then
  echo "FAIL the judge changed no file"
  FAILED=1
else
  echo "PASS the judge changed no file"
fi
exit "$FAILED"
