#!/usr/bin/env python3
"""Read the owner's judge line in a prompt and keep the session state in step.

Claude Code and Codex run this hook on UserPromptSubmit, SessionStart and SessionEnd: `judge_line.py <host>`
with the hook input as one JSON object on stdin. The first line of a prompt is a judge line when it is
`JUDGE off|gate|each [session]`, `JUDGE default` or `JUDGE round CON-YYYYMMDD-NN step|gate`. A mode line and
a round line go through scripts/readback_session.py only. The hook prints no blocking decision: it prints
one context line, or nothing. An event of a sub-agent is ignored.

Limits, stated on purpose: the event does not say who wrote a prompt. A judge line is recognized by its
text, and the absence of the sub-agent fields does not authenticate the owner. An unexpected exception
gives exit status 0 with empty streams.
"""

import json
import os
import re
import sys
import time

HOSTS = ("claude", "codex")
MODE_LINE = re.compile(r"JUDGE (off|gate|each)(?: session)?")
DEFAULT_LINE = "JUDGE default"
ROUND_LINE = re.compile(r"JUDGE round (CON-[0-9]{8}-[0-9]{2}) (step|gate)")
CLEAR_SOURCES = ("resume", "fork")
SUB_AGENT_FIELDS = ("agent_id", "agent_type")


def first_line(prompt):
    """The text before the first line feed, one trailing carriage return and leading spaces and tabs removed."""
    line = prompt.split("\n", 1)[0]
    if line.endswith("\r"):
        line = line[:-1]
    return line.lstrip(" \t")


def judge_line(prompt):
    """(kind, mode, lineage, tier) of a judge line, else None. kind is `mode` or `round`; mode None is `default`."""
    if not isinstance(prompt, str):
        return None
    line = first_line(prompt)
    if line == DEFAULT_LINE:
        return "mode", None, None, None
    found = MODE_LINE.fullmatch(line)
    if found:
        return "mode", found.group(1), None, None
    found = ROUND_LINE.fullmatch(line)
    if found:
        return "round", None, found.group(1), found.group(2)
    return None


def event_id_of(event):
    """`prompt:<prompt_id>`, else `turn:<turn_id>`, else None."""
    for key, prefix in (("prompt_id", "prompt:"), ("turn_id", "turn:")):
        value = event.get(key)
        if isinstance(value, str) and value != "":
            return prefix + value
    return None


def context(text):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                          "additionalContext": text}}, sort_keys=True) + "\n")
    sys.stdout.flush()


def prompt_event(host, session_id, event, session, settings):
    prompt = event.get("prompt")
    parsed = judge_line(prompt)
    if parsed is None:
        kind, state = session.read(host, session_id)
        if kind == "valid" and state["mode"] is not None and state["mode_event"] is not None:
            context("readback: judge mode %s for this session (owner's judge line of %s)"
                    % (state["mode"], state["mode_event"]["at"]))
        return
    kind, mode, lineage, tier = parsed
    event_id = event_id_of(event)
    if event_id is None:
        context("readback: the judge line was not stored: MISSING_EVENT_ID")
        return
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if kind == "mode":
        session.set_mode(host, session_id, mode, event_id, at)
        context("readback: processed judge-mode request for %s; duplicate delivery changes nothing"
                % ("default" if mode is None else mode))
        return
    cwd = event["cwd"] if isinstance(event.get("cwd"), str) else os.getcwd()
    try:
        found = settings.resolve(cwd, host)
        loaded = settings.load(found["home"])
    except settings.SettingsError as error:
        context("readback: the judge round was not stored: %s" % error.code)
        return
    session.authorize(host, session_id, event_id, at, found["home"], found["repository"], lineage, tier,
                      loaded["digest"])
    context("readback: processed judge-round request for %s %s; a duplicate delivery grants nothing more"
            % (lineage, tier))


def handle(host, event):
    name = event.get("hook_event_name")
    if name not in ("UserPromptSubmit", "SessionStart", "SessionEnd"):
        return
    if any(event.get(key) is not None for key in SUB_AGENT_FIELDS):
        return
    here = os.path.dirname(os.path.abspath(__file__))
    sys.dont_write_bytecode = True
    sys.path[:0] = [here, os.path.join(os.path.dirname(here), "scripts")]
    import readback_session
    import readback_settings
    session_id = event.get("session_id")
    if not readback_session.safe_id(session_id):
        return
    if name == "UserPromptSubmit":
        prompt_event(host, session_id, event, readback_session, readback_settings)
    elif name == "SessionEnd" or event.get("source") in CLEAR_SOURCES:
        readback_session.clear(host, session_id)


def main(argv):
    if len(argv) < 2 or argv[1] not in HOSTS:
        return 0
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if isinstance(event, dict):
            handle(argv[1], event)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
