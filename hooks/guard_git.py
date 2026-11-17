#!/usr/bin/env python3
"""Deny Git commands that can discard work that the agent did not make.

Claude Code runs this hook before each Bash call and passes the hook input as JSON on stdin.
A match prints a deny decision. Any other input prints nothing, so the call goes ahead.
The hook reads the command line only. A script that runs Git inside itself is not seen.
"""

import json
import re
import shlex
import sys


OPERATOR = set(";|&()<>")
WRAPPERS = {"command", "sudo", "env", "nohup", "time", "exec", "nice"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                          "--super-prefix", "--config-env"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
FALLBACK = re.compile(
    r"\bgit\b[^;&|\n]*?\b(reset\b[^;&|\n]*--hard|stash\b|clean\b|restore\b"
    r"|checkout\b[^;&|\n]*\s(--|\.)(\s|$)|switch\b[^;&|\n]*--discard-changes)")


def discarding(sub, args):
    """Return True when this Git subcommand can discard uncommitted work."""
    short = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    if sub == "reset":
        return "--hard" in args
    if sub == "checkout":
        return "--" in args or "." in args or "-f" in args or "--force" in args
    if sub == "switch":
        return "--discard-changes" in args or "-f" in args or "--force" in args
    if sub == "restore":
        staged = "--staged" in args or "S" in short
        worktree = "--worktree" in args or "W" in short
        return worktree or not staged
    if sub == "clean":
        return not ("--dry-run" in args or "n" in short)
    if sub == "stash":
        action = args[0] if args and not args[0].startswith("-") else "push"
        return action not in ("list", "show", "create", "store")
    return False


def git_call(words):
    """Return the denied command text for one simple command, or None."""
    words = list(words)
    while words and (ASSIGNMENT.match(words[0]) or words[0] in WRAPPERS or words[0].startswith("-")):
        words.pop(0)
    if not words:
        return None
    program = words[0].rsplit("/", 1)[-1]
    if program in SHELLS:
        for index, word in enumerate(words[1:-1], start=1):
            if word.startswith("-") and not word.startswith("--") and "c" in word:
                return check(words[index + 1])
        return None
    if program != "git":
        return None
    rest = words[1:]
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        if option in GIT_OPTIONS_WITH_VALUE and rest:
            rest.pop(0)
    if not rest:
        return None
    sub, args = rest[0], rest[1:]
    if discarding(sub, args):
        return " ".join(["git", sub, *args])[:120]
    return None


def check(command):
    """Return the first denied Git command inside a shell command line, or None."""
    text = command.replace("\n", " ; ").replace("`", " ; ").replace("$(", " ; ")
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        match = FALLBACK.search(command)
        return match.group(0)[:120] if match else None
    current = []
    for token in tokens + [";"]:
        if token and set(token) <= OPERATOR:
            found = git_call(current)
            if found:
                return found
            current = []
        else:
            current.append(token)
    return None


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, UnicodeError):
        return 0
    if not isinstance(event, dict) or event.get("tool_name") != "Bash":
        return 0
    command = (event.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return 0
    found = check(command)
    if not found:
        return 0
    if event.get("agent_type"):
        reason = (f"readback blocks `{found}`. It can discard changes that you did not make. "
                  "Leave them alone, name them in the handoff, and return BLOCKED with this command.")
    else:
        reason = (f"readback blocks `{found}`. It can discard changes that another person or agent made. "
                  "Tell the owner the command and the reason. The owner can run it outside the agent.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
