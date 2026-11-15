#!/bin/bash
# Two real sessions check the resume rule of the output style.
#
#   checks/resume.sh [plugin dir] [project name]
#
# Session 1 changes one file and leaves an open task that no file shows.
# Session 2 starts in a copy of the folder, so it has no memory of session 1.
# It must find the open task in ~/logbook/<project name>/CURRENT.md.
#
# The sessions run with your own Claude Code settings and cost model calls.
# Set RESUME_MODEL to choose the model.
# The script writes ~/logbook/<project name>/ and does not remove it.
set -u

PLUGIN="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
NAME="${2:-rb-resume-probe}"
BOOK="$HOME/logbook/$NAME"

if [ -e "$BOOK" ]; then
  echo "stop: $BOOK exists. Remove it or pass another project name."
  exit 2
fi
mkdir -p "$HOME/logbook"

TMP="$(mktemp -d)"
A="$TMP/a/$NAME"
B="$TMP/b/$NAME"
mkdir -p "$A" "$TMP/b"
printf '#!/bin/sh\necho "hello"\n' > "$A/greet.sh"

session() {  # folder, prompt, trace file
  (cd "$1" && claude -p "$2" \
     --plugin-dir "$PLUGIN" \
     --settings '{"outputStyle":"readback:Readback"}' \
     --permission-mode acceptEdits \
     --add-dir "$HOME/logbook" \
     --allowedTools "Bash(sh greet.sh)" \
     --output-format stream-json --verbose \
     ${RESUME_MODEL:+--model "$RESUME_MODEL"} > "$3" 2> "$3.err")
}

session "$A" 'Make greet.sh print "hello, world". The next task is to make it print the French "bonjour, monde". Do not do that now. Ask me before you start it.' "$TMP/s1.jsonl"
cp -R "$A" "$B"
session "$B" 'Continue where we stopped.' "$TMP/s2.jsonl"

python3 - "$TMP" "$BOOK" "$B" <<'PY'
import json, os, sys

tmp, book, folder = sys.argv[1:4]


def events(path):
    out = []
    for line in open(path, encoding="utf-8"):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def tool_inputs(evs):
    for e in evs:
        for block in ((e.get("message") or {}).get("content") or []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield json.dumps(block.get("input") or {})


def final(evs):
    for e in reversed(evs):
        if e.get("type") == "result":
            return str(e.get("result") or "")
    return ""


s2 = events(tmp + "/s2.jsonl")
current = os.path.join(book, "CURRENT.md")
wrote = os.path.exists(current)
rows = [("session 1 wrote CURRENT.md", wrote)]
if wrote:
    text = open(current, encoding="utf-8").read()
    rows.append(("CURRENT.md has at most 30 lines", len(text.splitlines()) <= 30))
    rows.append(("CURRENT.md holds the open task", "bonjour" in text.lower()))
greet = open(os.path.join(folder, "greet.sh"), encoding="utf-8").read()
rows.append(("session 1 did its task", "hello, world" in greet))
rows.append(("session 2 read CURRENT.md", any("CURRENT.md" in i for i in tool_inputs(s2))))
rows.append(("session 2 names the open task", "bonjour" in final(s2).lower()))
rows.append(("session 2 did not start the open task", "bonjour" not in greet.lower()))

for label, ok in rows:
    print("PASS" if ok else "FAIL", label)
print("records:", book)
print("traces:", tmp)
if not wrote:
    print("NOT RUN: session 1 wrote no CURRENT.md. Check the write permission for ~/logbook.")
    sys.exit(3)
sys.exit(0 if all(ok for _, ok in rows) else 1)
PY
