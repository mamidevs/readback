#!/usr/bin/env python3
"""Deny Git commands that can discard work that the agent did not make.

Claude Code and Codex run this hook before each Bash call and pass the hook input as JSON on stdin.
A match prints a deny decision. Any other input prints nothing, so the call goes ahead.
The deny check reads the command line only. A script that runs Git inside itself is not seen.
A checkout of one name counts as a path when it exists in the session, -C or --work-tree folder,
or when Git there does not resolve it as a revision. The hook does not follow cd or resolve Git aliases.
Codex does not pass the workdir of a command, so the session folder stands in for it.
A command that the hook does not deny and that holds a Git commit call is also observed: scripts/commit_observation.py
appends one line to a file below the Git common directory. It prints nothing and never changes the answer.
The host argument (`claude` or `codex`) comes from the registration. A second deny rule covers the owner's judge
settings and session state: a simple command that runs readback_settings.py with `set`, or with `migrate-teammates`
and `--write`, or that holds a word with `.readback/sessions`, is denied. It is a syntactic safety net with gaps.
"""

import json
import os
import re
import shlex
import subprocess
import sys


OPERATOR = set(";|&<>")
GROUPING = set("()")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                          "--super-prefix", "--config-env"}
CANDIDATE = re.compile(r"\bgit\b[^;&|\n]*?\bcommit\b")
FALLBACK = re.compile(
    r"\bgit\b[^;&|\n]*?\b(reset\b[^;&|\n]*--h|stash\b|clean\b|restore\b|rm\b|checkout\b|switch\b)")


def long_option(args, name):
    """True when an argument is the long option or an abbreviation that Git accepts for it."""
    for arg in args:
        option = arg.split("=", 1)[0]
        if option.startswith("--") and len(option) > 3 and name.startswith(option):
            return True
    return False


def short_flags(args):
    """The letters of every short option cluster, such as "qf" for -qf."""
    return "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--") and len(a) > 1)


def is_revision(base, name):
    """True when Git in base resolves name to a commit. Without Git or a repository, a missing path counts."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", base, "rev-parse", "--verify", "--quiet", name + "^{commit}"],
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return not os.path.lexists(os.path.join(base, name))
    if result.returncode in (0, 1):
        return result.returncode == 0
    return not os.path.lexists(os.path.join(base, name))


def discarding(sub, args, base):
    """Return True when this Git subcommand can discard uncommitted work."""
    paths = "--" in args
    options = args[:args.index("--")] if paths else args
    short = short_flags(options)
    force = "f" in short or long_option(options, "--force")
    if sub == "reset":
        return long_option(options, "--hard")
    if sub == "checkout":
        if force or paths or "." in options or long_option(options, "--pathspec-from-file"):
            return True
        if "b" in short or "B" in short or long_option(options, "--orphan"):
            return False
        names = [a for a in options if not a.startswith("-")]
        # Two names are a tree and a path. One name is a path when it exists or is not a revision.
        if len(names) >= 2:
            return True
        if len(names) == 1:
            return os.path.lexists(os.path.join(base, names[0])) or not is_revision(base, names[0])
        return False
    if sub == "switch":
        return force or long_option(options, "--discard-changes")
    if sub == "rm":
        return force
    if sub == "restore":
        staged = "S" in short or long_option(options, "--staged")
        worktree = "W" in short or long_option(options, "--worktree")
        return worktree or not staged
    if sub == "clean":
        return not ("n" in short or long_option(options, "--dry-run"))
    if sub == "stash":
        action = options[0] if options and not options[0].startswith("-") else "push"
        return action not in ("list", "show")
    return False


def git_call(rest, cwd):
    """Return the denied text for the words after `git`, or None."""
    rest = list(rest)
    base = cwd
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        name, _, value = option.partition("=")
        if name in GIT_OPTIONS_WITH_VALUE and not value and rest:
            value = rest.pop(0)
        if name in ("-C", "--work-tree") and value:
            base = os.path.join(base, value)
    if not rest:
        return None
    sub, args = rest[0], rest[1:]
    if discarding(sub, args, base):
        return " ".join(["git", sub, *args])[:120]
    return None


def simple_command(words, cwd):
    """Check one simple command. Git can follow wrappers such as env, timeout, nice or xargs."""
    for index, word in enumerate(words):
        program = word.rsplit("/", 1)[-1]
        if program == "git":
            found = git_call(words[index + 1:], cwd)
            if found:
                return found
        elif program == "eval":
            return check(" ".join(words[index + 1:]), cwd)
        elif program in SHELLS:
            for position in range(index + 1, len(words) - 1):
                option = words[position]
                if option.startswith("-") and not option.startswith("--") and "c" in option:
                    found = check(words[position + 1], cwd)
                    if found:
                        return found
                    break
    return None


def substitutions(command):
    """Yield the text inside each $(...) and each backtick pair, also inside double quotes."""
    start = command.find("$(")
    while start != -1:
        depth, index = 1, start + 2
        while index < len(command) and depth:
            depth += {"(": 1, ")": -1}.get(command[index], 0)
            index += 1
        yield command[start + 2:index - 1 if depth == 0 else index]
        start = command.find("$(", start + 2)
    yield from re.findall(r"`([^`]*)`", command)


def check(command, cwd="."):
    """Return the first denied Git command inside a shell command line, or None."""
    for inner in substitutions(command):
        found = check(inner, cwd)
        if found:
            return found
    text = command.replace("\n", " ; ").replace("`", " ")
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        match = FALLBACK.search(command)
        return match.group(0)[:120] if match else None
    current = []
    for token in tokens + [";"]:
        if token and set(token) <= GROUPING:
            continue
        if token and set(token) <= OPERATOR:
            found = simple_command(current, cwd)
            if found:
                return found
            current = []
        else:
            current.append(token)
    return None


def simple_commands(command):
    """Return the simple commands of a command line as word lists, in walk order.

    Each $(...) and backtick substitution is visited first, innermost first, then the line. The body of a
    visited substitution is removed from the text that encloses it, so each Git occurrence counts once.
    The words after eval and the -c string of a shell are walked where they stand. A text that shlex
    cannot split gives no simple command. A word of digits only before an operator that starts with < or >
    is a file descriptor and is dropped. Return (simple commands, True when every visited text was split).
    """
    commands, pieces, position, failures = [], [], 0, []
    start = command.find("$(")
    while start != -1:
        depth, index = 1, start + 2
        while index < len(command) and depth:
            depth += {"(": 1, ")": -1}.get(command[index], 0)
            index += 1
        end = index - 1 if depth == 0 else index
        inner, whole = simple_commands(command[start + 2:end])
        commands.extend(inner)
        failures.extend([] if whole else [True])
        pieces.append(command[position:start + 2])
        position = end
        start = command.find("$(", end)
    pieces.append(command[position:])

    def visit(match):
        inner, whole = simple_commands(match.group(1))
        commands.extend(inner)
        failures.extend([] if whole else [True])
        return " "

    text = re.sub(r"`([^`]*)`", visit, "".join(pieces)).replace("\n", " ; ").replace("`", " ")
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return commands, False
    current = []
    for token in tokens + [";"]:
        if token and set(token) <= GROUPING:
            continue
        if not (token and set(token) <= OPERATOR):
            current.append(token)
            continue
        if token[0] in "<>" and current and current[-1].isascii() and current[-1].isdigit():
            current.pop()
        words, nested, skipped = current, [], set()
        current = []
        for index, word in enumerate(words):
            program = word.split("/")[-1]
            if program == "eval":
                nested.append(" ".join(words[index + 1:]))
                words = words[:index + 1]
                break
            if program in SHELLS:
                for option in range(index + 1, len(words) - 1):
                    if words[option].startswith("-") and not words[option].startswith("--") and "c" in words[option]:
                        nested.append(words[option + 1])
                        skipped.add(option + 1)
                        break
        if words:
            commands.append([word for index, word in enumerate(words) if index not in skipped])
        for text in nested:
            inner, whole = simple_commands(text)
            commands.extend(inner)
            failures.extend([] if whole else [True])
    return commands, not failures


def holds_commit(commands):
    """True when a simple command holds a word git that Git's global options and then the word commit follow."""
    for words in commands:
        for index, word in enumerate(words):
            if word.split("/")[-1] != "git":
                continue
            position = index + 1
            while position < len(words) and words[position].startswith("-"):
                name, equals, _ = words[position].partition("=")
                position += 2 if name in GIT_OPTIONS_WITH_VALUE and not equals else 1
            if position < len(words) and words[position] == "commit":
                return True
    return False


def settings_text(commands):
    """The text of the first simple command that changes the judge settings or touches the session state, or None."""
    for words in commands:
        matched = any(".readback/sessions" in word for word in words)
        for index, word in enumerate(words):
            if word.split("/")[-1] != "readback_settings.py":
                continue
            later = words[index + 1:]
            if "set" in later or ("migrate-teammates" in later and "--write" in later):
                matched = True
        if matched:
            return " ".join(words)[:120]
    return None


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


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
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else "."
    host = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("claude", "codex") else None
    found = check(command, cwd)
    if not found:
        try:
            commands, whole = simple_commands(command)
            owned = settings_text(commands)
            if owned:
                deny(f"readback blocks {owned}. Only the owner changes the judge settings and the session state. "
                     "Tell the owner the command; the owner can run it in a terminal.")
                return 0
            if os.environ.get("READBACK_COMMIT_CHECK") != "off" and (
                    holds_commit(commands) or (not whole and CANDIDATE.search(command))):
                here = os.path.dirname(os.path.abspath(__file__))
                sys.dont_write_bytecode = True
                sys.path[:0] = [here, os.path.join(os.path.dirname(here), "scripts")]
                import commit_observation
                if host is None:
                    commit_observation.observe(commands, whole, command, event)
                else:
                    commit_observation.observe(commands, whole, command, event, host)
        except Exception:
            pass
        return 0
    if event.get("agent_type"):
        reason = (f"readback blocks `{found}`. It can discard changes that you did not make. "
                  "Leave them alone, name them in the handoff, and return BLOCKED with this command.")
    else:
        reason = (f"readback blocks `{found}`. It can discard changes that another person or agent made. "
                  "Tell the owner the command and the reason. The owner can run it outside the agent.")
    deny(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
