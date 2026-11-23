#!/usr/bin/env python3
"""PreToolUse hook that denies a readback:worker dispatch whose prompt names a contract and does not deliver it.

Usage: guard_dispatch.py claude

The hook reads the event from stdin. For tool_name Agent or Task with subagent_type readback:worker it
looks for CON ids in the prompt. A CON id is CON-<8 digits>-<2 to 6 digits>. An id is delivered when the
prompt holds a path token that ends with <id>.md (an absolute path or a path that starts with ~ must name an
existing file; a relative path is accepted), or when the prompt holds at least one statement line: ACC-<n> at
the start of a line, after optional spaces, tabs, >, |, list markers or *, followed by at least three words.
For each undelivered id the hook prints one deny decision in the form of guard_git.py.

Limits:
- The hook reads the prompt only. It does not read the contract record and does not check its content.
- The hook does not show that the worker read the contract.
- The hook does not run in Codex; a Codex lead has the text rules only.
- Any malformed event, other tool, other sub-agent type or exception gives exit status 0 and empty output.
"""

import json
import os
import re
import sys

ID = re.compile(r"(?<![A-Za-z0-9_])CON-\d{8}-\d{2,6}(?![A-Za-z0-9_])")
STATEMENT = re.compile(r"^[ \t>|*+\-•]*(?:\d+[.)][ \t]+)?ACC-\d+(?![A-Za-z0-9_-])(.*)$")
EDGE = "`'\"()[]{}<>"
TRAILING = ".,;:!?"


def prompt_ids(prompt):
    seen = []
    for found in ID.findall(prompt):
        if found not in seen:
            seen.append(found)
    return seen


def clean(token):
    old = None
    while old != token:
        old = token
        token = token.strip(EDGE).rstrip(TRAILING)
    return token


def path_delivers(prompt, one):
    suffix = one + ".md"
    for raw in prompt.split():
        token = clean(raw)
        if not token.endswith(suffix):
            continue
        if token.startswith("/") or token.startswith("~"):
            try:
                if os.path.isfile(os.path.expanduser(token)):
                    return True
            except (ValueError, OSError):
                pass
            continue
        return True
    return False


def has_statement(prompt):
    for line in prompt.splitlines():
        match = STATEMENT.match(line)
        if not match:
            continue
        words = [w for w in match.group(1).split() if re.search(r"\w", w)]
        if len(words) >= 3:
            return True
    return False


def find_undelivered(prompt):
    ids = prompt_ids(prompt)
    if not ids or has_statement(prompt):
        return []
    return [one for one in ids if not path_delivers(prompt, one)]


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


def main():
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict) or event.get("tool_name") not in ("Agent", "Task"):
            return 0
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict) or tool_input.get("subagent_type") != "readback:worker":
            return 0
        prompt = tool_input.get("prompt")
        if not isinstance(prompt, str):
            return 0
        missing = find_undelivered(prompt)
        if not missing:
            return 0
        names = ", ".join(missing)
        reason = (f"readback blocks this dispatch: the prompt names {names} and delivers no statement of it. "
                  "A contract id alone delivers no statement. Put the complete contract in the worker's prompt "
                  "(its ACC statements with their checks and the out-of-scope line), or add the path of the "
                  "record, which ends with <id>.md, and send the dispatch again.")
        deny(reason)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
