#!/usr/bin/env python3
"""Observe a Git commit call that the guard hook sees, and record one line about it.

The observation describes. It decides nothing, and it never changes what the hook answers.
The module has two parts. The pure part reads the simple commands that the hook found and builds
values from them: the commit call, `commit_count`, `target`, the base folder, the `commit` object,
the line object and the bytes of the line. It reads no file, starts no process and reads no clock.
The store part resolves the Git common directory, takes the capture, looks up the review records,
keeps the 5 second budget and appends the line to `<git common dir>/readback/commit-observations.jsonl`.

Limits, stated on purpose: the hook sees the command line before the Bash call starts. It does not
follow `cd`, resolve Git aliases or read scripts. A word `git` after another program counts as a call.
The count is of syntactic occurrences, not of executions. The budget is cooperative. The records and
the observation file are local and untrusted. The line copies no argument value of the command line.
"""

import json
import os
import re
import stat
import time

import review_capture

SCHEMA_VERSION = 1
LINE_SCHEMA_VERSION = 2
BUDGET_SECONDS = 5
# The same set as GIT_OPTIONS_WITH_VALUE of hooks/guard_git.py. A test compares the two.
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                          "--super-prefix", "--config-env"}
TARGET_OPTIONS = ("--git-dir", "--work-tree", "--namespace")
DIRECTORY_COMMANDS = ("cd", "pushd", "popd")
SHORT_WITH_VALUE = {"m": "message", "F": "file", "C": "reuse", "c": "reuse", "t": None}
SHORT_WITH_REST = "Su"
LONG_WITH_VALUE = {
    "--message": "message", "--file": "file", "--reuse-message": "reuse", "--reedit-message": "reuse",
    "--fixup": "fixup", "--squash": "squash", "--author": None, "--date": None, "--template": None,
    "--cleanup": None, "--trailer": None, "--pathspec-from-file": None,
}
MARKER_NAME = re.compile(r"[0-9a-f]{32}")
COMPLETED_SUFFIX = ".completed.json"
# A failed-parse candidate: the same pattern as CANDIDATE in hooks/guard_git.py. A test compares the two.
CANDIDATE = re.compile(r"\bgit\b[^;&|\n]*?\bcommit\b")

# Module-level names so that tests can replace the clock and the file system calls.
_clock = time.monotonic
_lstat = os.lstat
_listdir = os.listdir
_open_file = open
_os_open = os.open
_fchmod = os.fchmod
_write = os.write


# ------------------------------------------------------------------ pure part

def commit_call_in(words):
    """The commit call of one simple command, or None.

    A word whose last path part is `git`, followed after Git's global options by the word `commit`.
    The result holds the words before `git`, the global options as (name, value) pairs and the words after `commit`.
    """
    for index, word in enumerate(words):
        if word.split("/")[-1] != "git":
            continue
        options, position = [], index + 1
        while position < len(words) and words[position].startswith("-"):
            name, equals, value = words[position].partition("=")
            position += 1
            if name in GIT_OPTIONS_WITH_VALUE and not equals and position < len(words):
                value = words[position]
                position += 1
            options.append((name, value))
        if position < len(words) and words[position] == "commit":
            return {"before": words[:index], "options": options, "args": words[position + 1:]}
    return None


def find_commit(commands):
    """(commit_count, first commit call or None). The first call carries the position of its simple command."""
    count, first = 0, None
    for position, words in enumerate(commands):
        call = commit_call_in(words)
        if call is None:
            continue
        count += 1
        if first is None:
            first = dict(call, position=position)
    return count, first


def target_of(commands, call):
    """`unknown` when the commit may go to a repository that the folder does not name, else `resolved`."""
    if any(name in TARGET_OPTIONS for name, _ in call["options"]):
        return "unknown"
    if any(word.startswith("GIT_") and "=" in word for word in call["before"]):
        return "unknown"
    if any(words and words[0] in DIRECTORY_COMMANDS for words in commands[:call["position"]]):
        return "unknown"
    return "resolved"


def base_folder(cwd, call):
    """The folder that the commit works in: cwd with each global -C joined in order."""
    base = cwd
    for name, value in call["options"]:
        if name == "-C":
            base = os.path.join(base, value)
    return base


def commit_object(args):
    """The `commit` object of the words after `commit`. No value of an argument enters it."""
    everything, amend, dry_run = False, False, False
    source, pathspecs, from_file, ended = None, 0, False, False
    index = 0
    while index < len(args):
        word = args[index]
        index += 1
        if ended:
            pathspecs += 1
        elif word == "--":
            ended = True
        elif word.startswith("--"):
            name, equals, _ = word.partition("=")
            if name in LONG_WITH_VALUE:
                if not equals:
                    index += 1
                if source is None:
                    source = LONG_WITH_VALUE[name]
                if name == "--pathspec-from-file":
                    from_file = True
            elif word == "--all":
                everything = True
            elif word == "--amend":
                amend = True
            elif word == "--dry-run":
                dry_run = True
        elif word.startswith("-") and len(word) > 1:
            cluster, position = word[1:], 0
            while position < len(cluster):
                letter = cluster[position]
                position += 1
                if letter == "a":
                    everything = True
                elif letter in SHORT_WITH_VALUE:
                    if position == len(cluster):
                        index += 1
                    position = len(cluster)
                    if source is None:
                        source = SHORT_WITH_VALUE[letter]
                elif letter in SHORT_WITH_REST:
                    position = len(cluster)
        else:
            pathspecs += 1
    return {"all": everything, "amend": amend, "dry_run": dry_run,
            "message_source": source or "none", "pathspecs": None if from_file else pathspecs}


def parse_of(count, whole, command):
    """`ok`, `partial` or `failed` for an observation candidate, else None.

    `count` is the number of recognized commit calls, `whole` says that every visited text was split.
    """
    if count:
        return "ok" if whole else "partial"
    if not whole and CANDIDATE.search(command):
        return "failed"
    return None


def cwd_of(event):
    """The event's `cwd` when it is a string, else `.`."""
    return event["cwd"] if isinstance(event.get("cwd"), str) else "."


def sub_agent_of(event):
    """True when the event's `agent_type` is a non-empty string."""
    return isinstance(event.get("agent_type"), str) and event["agent_type"] != ""


def capture_object(state, reason, manifest, index):
    return {"state": state, "reason": reason, "manifest_sha256": manifest, "index_sha256": index}


def line_object(parse, moment, sub_agent, count, commit, target, capture, reviews, reviews_state, unmatched, elapsed_ms):
    """The line, with exactly the keys of the schema."""
    return {
        "schema_version": LINE_SCHEMA_VERSION, "parse": parse, "time": moment, "sub_agent": sub_agent, "commit_count": count,
        "commit": commit, "target": target, "capture": capture, "reviews": reviews,
        "reviews_state": reviews_state, "unmatched_markers": unmatched, "elapsed_ms": elapsed_ms,
        "eligibility": "UNVERIFIED",
    }


def serialize(line):
    """Sorted keys, separators `,` and `:`, ASCII only, one line feed at the end."""
    return (json.dumps(line, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


# ----------------------------------------------------------------- store part

class _OutOfTime(Exception):
    """The budget ended during the lookup."""


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _expired(start):
    return _clock() - start >= BUDGET_SECONDS


def _text(value):
    return value if isinstance(value, str) else None


def _member(record, container, field):
    inner = record.get(container)
    return _text(inner.get(field)) if isinstance(inner, dict) else None


def _record(reviews_dir, key, marker, start):
    """The entry of the completed record of this marker in this key folder, or None when there is no valid one."""
    path = os.path.join(reviews_dir, key, marker + COMPLETED_SUFFIX)
    try:
        info = _lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    if _expired(start):
        raise _OutOfTime()
    with _open_file(path, "rb") as handle:
        try:
            data = json.load(handle)
        except (ValueError, RecursionError):
            return None
    if not isinstance(data, dict):
        return None
    version = data.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION or data.get("attempt_id") != marker:
        return None
    return {
        "attempt_id": marker, "review_key": key, "created": _text(data.get("created")),
        "verdict": _member(data, "judge", "verdict"), "binding": _text(data.get("binding")),
        "contract_sha256": _member(data, "contract", "sha256"), "handoff_sha256": _member(data, "handoff", "sha256"),
    }


def _lookup(common, manifest, start):
    """(entries, number of markers without a valid record). It raises on an I/O error or at the end of the budget."""
    reviews_dir = os.path.join(common, "readback", "reviews")
    try:
        names = _listdir(os.path.join(common, "readback", "by-manifest", manifest))
    except FileNotFoundError:
        return [], 0
    markers = sorted(name for name in names if MARKER_NAME.fullmatch(name))
    if not markers:
        return [], 0
    keys = sorted(name for name in _listdir(reviews_dir) if os.path.isdir(os.path.join(reviews_dir, name)))
    entries, unmatched = [], 0
    for marker in markers:
        if _expired(start):
            raise _OutOfTime()
        found = [entry for entry in (_record(reviews_dir, key, marker, start) for key in keys) if entry is not None]
        entries.extend(found)
        if not found:
            unmatched += 1
    entries.sort(key=lambda entry: (entry["created"] or "", entry["attempt_id"], entry["review_key"]))
    return entries, unmatched


def _reviews(common, manifest, start):
    """(reviews_state, reviews, unmatched_markers) for a capture that is ok."""
    try:
        entries, unmatched = _lookup(common, manifest, start)
    except _OutOfTime:
        return "timeout", None, None
    except Exception:
        return "error", None, None
    if _expired(start):
        return "timeout", None, None
    return "ok", entries, unmatched


def _identify(base, common, start):
    """(capture, reviews, reviews_state, unmatched_markers) of a resolved target."""
    left = BUDGET_SECONDS - (_clock() - start)
    if left <= 0:
        return capture_object("timeout", "budget ended before the capture", None, None), None, "no-identity", None
    try:
        result = review_capture.capture(base, left)
        capture = capture_object(result["state"], result["reason"], result["manifest_sha256"], result["index_sha256"])
    except Exception:
        return capture_object("error", "capture raised", None, None), None, "no-identity", None
    if capture["state"] != "ok":
        return capture, None, "no-identity", None
    state, reviews, unmatched = _reviews(common, capture["manifest_sha256"], start)
    return capture, reviews, state, unmatched


def _append(path, data):
    """Create the file exclusively with mode 0600, or append to the file that exists. One write, no rollback."""
    try:
        fd = _os_open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd, created = _os_open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW), False
    else:
        created = True
    try:
        if created:
            _fchmod(fd, 0o600)
        _write(fd, data)
    finally:
        os.close(fd)


def commit_check_off(base, host):
    """True only when the project of the base folder has an effective `commit_check` of `off`. A failure gives False."""
    try:
        import readback_settings
        found = readback_settings.resolve(base, host)
        return readback_settings.load(found["home"])["settings"]["commit_check"] == "off"
    except Exception:
        return False


def observe(commands, whole, command, event, host=None):
    """Append one line for the first commit call, or for a failed-parse candidate. Return the file's path, or None.

    `commands` is the list of word lists of the command line, `whole` says that every visited text was split,
    `command` is the raw command line and `event` is the hook input. With a `host`, a project whose `commit_check`
    is `off` gets no capture and no line; without one the switch is skipped.
    """
    count, call = find_commit(commands)
    parse = parse_of(count, whole, command)
    if parse is None:
        return None
    if parse == "failed":
        base, target, commit, count = cwd_of(event), "unknown", None, None
    else:
        base, target = base_folder(cwd_of(event), call), target_of(commands, call)
        commit = commit_object(call["args"])
    start = _clock()
    common = review_capture.git_common_dir(base)
    if common is None or not os.path.isdir(os.path.join(common, "readback", "reviews")):
        return None
    if host is not None and commit_check_off(base, host):
        return None
    if target == "unknown":
        capture, reviews, reviews_state, unmatched = None, None, "no-identity", None
    else:
        capture, reviews, reviews_state, unmatched = _identify(base, common, start)
    moment = _now()
    sample = _clock()
    line = line_object(parse, moment, sub_agent_of(event), count, commit, target,
                       capture, reviews, reviews_state, unmatched, int((sample - start) * 1000))
    path = os.path.join(common, "readback", "commit-observations.jsonl")
    data = serialize(line)
    try:
        _append(path, data)
    except OSError:
        return None
    return path
