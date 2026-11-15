#!/bin/bash
# Sends one HANDOFF and its CONTRACT to a judge at another provider.
#
#   scripts/outside-judge.sh <contract file> <handoff file> [repository folder]
#
# The judge is the first `judge` row of the teammates table:
#   ~/logbook/TEAMMATES.md, or the file that READBACK_TEAMMATES names.
# Only the provider `codex` is supported. The call is read-only.
# The other provider sees the contract, the handoff and the repository folder.
#
# Output, first two lines:
#   JUDGE      <PASS, FAIL or none> | <provider:model> | <repo@sha or none> | <note>
#   execution  <provider> | <model requested> | <effort requested> | cli | none
# Then the judge's answer.
#
# Exit codes: 0 a verdict arrived. 2 no outside judge is named. 3 the call gave no verdict.
# The script never retries and never changes the model.
set -u

CONTRACT="${1:-}"
HANDOFF="${2:-}"
REPO="${3:-$PWD}"
TABLE="${READBACK_TEAMMATES:-$HOME/logbook/TEAMMATES.md}"
SECONDS_MAX="${READBACK_JUDGE_SECONDS:-300}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -f "$CONTRACT" ] || [ ! -f "$HANDOFF" ]; then
  echo "usage: outside-judge.sh <contract file> <handoff file> [repository folder]" >&2
  exit 64
fi
if [ ! -f "$TABLE" ]; then
  echo "no outside judge is named: $TABLE does not exist"
  exit 2
fi

ROW="$(grep -E '^[[:space:]]*judge[[:space:]]*\|' "$TABLE" | head -1)"
if [ -z "$ROW" ]; then
  echo "no outside judge is named: $TABLE has no judge row"
  exit 2
fi
field() { printf '%s' "$ROW" | cut -d'|' -f"$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }
PROVIDER="$(field 2)"
MODEL="$(field 3)"
EFFORT="$(field 4)"
if [ "$PROVIDER" != "codex" ] || [ -z "$MODEL" ] || [ -z "$EFFORT" ]; then
  echo "no outside judge is named: the judge row needs codex, a model and an effort"
  exit 2
fi

SHA="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || true)"
if [ -n "$SHA" ]; then WHERE="$(basename "$(git -C "$REPO" rev-parse --show-toplevel)")@$SHA"; else WHERE="none"; fi
EXECUTION="execution  $PROVIDER | $MODEL | $EFFORT | cli | none"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
{
  awk 'BEGIN{n=0} /^---$/ && n<2 {n++; next} n>=2 {print}' "$HERE/agents/judge.md"
  printf '\nJudge this handoff against its contract. The files are in the working folder.\n\n'
  cat "$CONTRACT"
  printf '\n'
  cat "$HANDOFF"
} > "$TMP/prompt.txt"

none() {  # reason
  echo "JUDGE      none | $PROVIDER:$MODEL | $WHERE | no verdict: $1"
  echo "$EXECUTION"
  exit 3
}

command -v codex >/dev/null 2>&1 || none "the codex command is not installed"

# stdin is closed: an open stdin makes `codex exec` wait for more input.
codex exec --ignore-user-config --ignore-rules \
  -m "$MODEL" -c "model_reasoning_effort=\"$EFFORT\"" \
  -s read-only -C "$REPO" --skip-git-repo-check --ephemeral \
  -o "$TMP/answer.txt" "$(cat "$TMP/prompt.txt")" < /dev/null > "$TMP/run.log" 2>&1 &
PID=$!
# The watchdog holds no pipe of the caller, so a caller that reads our output does not wait for it.
( sleep "$SECONDS_MAX"; kill "$PID" 2>/dev/null ) > /dev/null 2>&1 < /dev/null &
WATCH=$!
wait "$PID" 2>/dev/null
CODE=$?
pkill -P "$WATCH" 2>/dev/null
kill "$WATCH" 2>/dev/null
wait "$WATCH" 2>/dev/null

[ "$CODE" -eq 0 ] || none "the call ended with code $CODE after at most $SECONDS_MAX seconds"
[ -s "$TMP/answer.txt" ] || none "the call returned no answer"

# Only the first word of the final answer counts.
WORD="$(sed -e 's/^[[:space:]`*>#]*//' "$TMP/answer.txt" | grep -m1 -E '.' | sed -E 's/^([A-Za-z]+).*/\1/')"
case "$WORD" in
  PASS|FAIL) ;;
  *) none "the answer does not start with PASS or FAIL" ;;
esac

echo "JUDGE      $WORD | $PROVIDER:$MODEL | $WHERE | see the answer below"
echo "$EXECUTION"
echo
cat "$TMP/answer.txt"
exit 0
