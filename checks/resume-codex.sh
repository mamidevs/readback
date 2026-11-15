#!/bin/bash
# Two sessions check the resume rule with OpenAI Codex as the lead.
#
#   checks/resume-codex.sh [skills folder]
#
# Session 1 changes one file and leaves an open task that no file shows.
# Session 2 starts in a copy of the folder. It must find the open task in CURRENT.md.
# The project holds the portable skills in .agents/skills and one line in AGENTS.md.
#
# The sessions get a temporary home folder. The records go to a temporary ~/logbook,
# and the skills of your own home folder are not visible. Your Codex login is used,
# and Codex still reads the AGENTS.md of your Codex home.
# Set RESUME_MODEL and RESUME_EFFORT to choose the model.
# Set RESUME_CONTROL=1 to run without the skills and without the AGENTS.md line.
set -u

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SKILLS="${1:-$HERE/portable/skills}"
MODEL="${RESUME_MODEL:-gpt-6-astra}"
EFFORT="${RESUME_EFFORT:-high}"
DEADLINE="${RESUME_SECONDS:-600}"
REAL_CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

TMP="$(mktemp -d)"
export HOME="$TMP/home"
export CODEX_HOME="$REAL_CODEX_HOME"
NAME="rb-resume-probe"
A="$TMP/a/$NAME"
B="$TMP/b/$NAME"
BOOK="$HOME/logbook/$NAME"
mkdir -p "$A" "$TMP/b" "$HOME/logbook"
printf '#!/bin/sh\necho "hello"\n' > "$A/greet.sh"

if [ "${RESUME_CONTROL:-0}" != "1" ]; then
  mkdir -p "$A/.agents"
  cp -R "$SKILLS" "$A/.agents/skills"
  printf 'For every task, and when I ask to continue earlier work, load the skill `readback` and follow it.\n' > "$A/AGENTS.md"
fi

session() {  # folder, sandbox, prompt, name
  codex exec --ignore-user-config --ignore-rules \
    -m "$MODEL" -c "model_reasoning_effort=\"$EFFORT\"" \
    -s "$2" --add-dir "$HOME/logbook" -C "$1" --skip-git-repo-check --ephemeral \
    --json -o "$TMP/$4.txt" "$3" < /dev/null > "$TMP/$4.jsonl" 2> "$TMP/$4.err" &
  pid=$!
  ( sleep "$DEADLINE"; kill "$pid" 2>/dev/null ) > /dev/null 2>&1 < /dev/null &
  watch=$!
  wait "$pid" 2>/dev/null
  pkill -P "$watch" 2>/dev/null
  kill "$watch" 2>/dev/null
  wait "$watch" 2>/dev/null
}

session "$A" workspace-write 'Make greet.sh print "hello, world". The next task is to make it print the French "bonjour, monde". Do not do that now. Ask me before you start it.' s1
cp -R "$A" "$B"
session "$B" read-only 'Continue where we stopped.' s2

python3 - "$TMP" "$BOOK" "$B" <<'PY'
import json, os, sys

tmp, book, folder = sys.argv[1:4]


def commands(path):
    out = []
    for line in open(path, encoding="utf-8"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        item = e.get("item") or {}
        if e.get("type") == "item.completed":
            out.append(json.dumps(item))
    return out


def read(path):
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


current = os.path.join(book, "CURRENT.md")
wrote = os.path.exists(current)
rows = [("session 1 wrote CURRENT.md", wrote)]
if wrote:
    text = read(current)
    rows.append(("CURRENT.md has at most 30 lines", len(text.splitlines()) <= 30))
    rows.append(("CURRENT.md holds the open task", "bonjour" in text.lower()))
greet = read(os.path.join(folder, "greet.sh"))
rows.append(("session 1 did its task", "hello, world" in greet))
rows.append(("session 2 read CURRENT.md", any("CURRENT.md" in c for c in commands(tmp + "/s2.jsonl"))))
rows.append(("session 2 names the open task", "bonjour" in read(tmp + "/s2.txt").lower()))
rows.append(("session 2 did not start the open task", "bonjour" not in greet.lower()))

for label, ok in rows:
    print("PASS" if ok else "FAIL", label)
print("records:", book)
print("traces:", tmp)
if not wrote:
    print("NOT RUN: session 1 wrote no CURRENT.md.")
    sys.exit(3)
sys.exit(0 if all(ok for _, ok in rows) else 1)
PY
