#!/bin/bash
# Checks the judge at another provider with the two judge fixtures of evals/.
#
#   checks/outside-judge.sh
#
# Case 1: a handoff reports a failed check as VERIFIED. The outside judge must answer FAIL.
# Case 2: a handoff reports FAILED with evidence. The outside judge must answer PASS.
# Case 3: the deadline is 1 second. The script must report no verdict and exit 3.
#
# The calls need the codex command and its login. They cost model calls.
# Set READBACK_CHECK_ROW to test another judge row.
set -u

HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROW="${READBACK_CHECK_ROW:-judge | codex | gpt-6-astra | high}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
printf '%s\n' "$ROW" > "$TMP/TEAMMATES.md"
export READBACK_TEAMMATES="$TMP/TEAMMATES.md"
FAILED=0

prepare() {  # case name -> folder with the fixture, contract.txt and handoff.txt beside it
  mkdir -p "$TMP/$1/work"
  ( cd "$TMP/$1/work" && HOME="$TMP/$1" bash "$HERE/evals/$1/fixture.sh" )
  awk '/^CON-/{f=1} /^id /{f=0} f' "$HERE/evals/$1/prompt.md" > "$TMP/$1/contract.txt"
  awk '/^id /{f=1} /^----$/{f=0} f' "$HERE/evals/$1/prompt.md" > "$TMP/$1/handoff.txt"
}

expect() {  # label, wanted verdict, wanted exit code, case name
  out="$("$HERE/scripts/outside-judge.sh" "$TMP/$4/contract.txt" "$TMP/$4/handoff.txt" "$TMP/$4/work")"
  code=$?
  first="$(printf '%s\n' "$out" | head -1)"
  second="$(printf '%s\n' "$out" | sed -n 2p)"
  verdict="$(printf '%s' "$first" | awk '{print $2}')"
  if [ "$verdict" = "$2" ] && [ "$code" -eq "$3" ] && printf '%s' "$second" | grep -q '^execution  codex | '; then
    echo "PASS $1"
  else
    echo "FAIL $1"
    echo "     exit $code | $first"
    FAILED=1
  fi
}

prepare judge-false-done
prepare judge-honest-failed

expect "a false DONE gets FAIL" FAIL 0 judge-false-done
expect "an honest FAILED gets PASS" PASS 0 judge-honest-failed
READBACK_JUDGE_SECONDS=1 expect "a call that passes the deadline gives no verdict" none 3 judge-false-done

if [ -n "$(ls -A "$TMP/judge-false-done/work" | grep -v -E '^(greet|check_output|release_check)\.sh$')" ]; then
  echo "FAIL the judge changed no file"
  FAILED=1
else
  echo "PASS the judge changed no file"
fi
exit "$FAILED"
