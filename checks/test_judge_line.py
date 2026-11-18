#!/usr/bin/env python3
"""Offline checks for the judge line hook, the deny rule of the Git guard and the commit check switch.

Run one class or several: python3 -B checks/test_judge_line.py LineTests HookTests GuardTests ConfigTests
Every process that a test starts gets its own HOME below a temporary folder and pinned session variables.
"""

import calendar
import concurrent.futures
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "900e2ecaa4953be5e4e110c7b119af2a3879131b"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "checks"))
_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True
import readback_session as rs  # noqa: E402
import readback_settings as rsettings  # noqa: E402
import test_commit_observation as tco  # noqa: E402
from test_guard_git import ALLOWED, DENIED  # noqa: E402
sys.dont_write_bytecode = _bytecode

COMMIT_OBSERVATION_TESTS_SHA256 = "6005c2a47cd33f3981aaccac5cdeb5c12c950111606002a39e6b850b43bd9cf6"
SID = "s1"
FIXED_AT = "2026-11-17T01:02:03Z"
AT_FORM = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
HOSTS = ("claude", "codex")
LAYOUTS = ("plugin", "portable")
ROUND = "JUDGE round CON-20261118-01 gate"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = before
    return module


def scratch(test):
    """A fresh real path below the temporary folder, removed after the test."""
    path = os.path.realpath(tempfile.mkdtemp(prefix="jl-"))
    test.addCleanup(tco.remove, path)
    return path


def make_layout(kind, dest, drop=(), stubs=None):
    """The plugin layout (hooks/ and scripts/) or the portable layout (one folder). Return the hook folder."""
    dest = Path(dest)
    if kind == "plugin":
        hooks, scripts, source = dest / "hooks", dest / "scripts", ROOT / "scripts"
        hooks.mkdir(parents=True)
        scripts.mkdir(parents=True)
        for name in ("judge_line.py", "guard_git.py"):
            shutil.copyfile(ROOT / "hooks" / name, hooks / name)
    else:
        hooks = scripts = dest
        source = ROOT / "portable" / "skills" / "readback" / "scripts"
        dest.mkdir(parents=True)
    for path in source.glob("*.py"):
        if path.name not in drop:
            shutil.copyfile(path, scripts / path.name)
    for name, data in (stubs or {}).items():
        (scripts / name).write_bytes(data)
    return hooks


def environment(home, **extra):
    """The environment of a hook process: its own HOME, pinned session variables, no Git variables."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") and key not in ("READBACK_COMMIT_CHECK", "PYTHONDONTWRITEBYTECODE", "HOME",
                                                         "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID")}
    env.update(HOME=home, CLAUDE_CODE_SESSION_ID="from-env-claude", CODEX_SESSION_ID="from-env-codex")
    env.update(extra)
    return env


def run_hook(script, host, event=None, raw=None, env=None, cwd=None):
    data = raw if raw is not None else json.dumps(event).encode("utf-8")
    command = [sys.executable, str(script)] + ([host] if host else [])
    return subprocess.run(command, input=data, capture_output=True, env=env, cwd=cwd, timeout=60)


# ===================================================================================== LineTests

# (id, prompt, expected): None, or (kind, mode, lineage, tier)
LINE_ROWS = [
    ("J1", "JUDGE off", ("mode", "off", None, None)),
    ("J2", "JUDGE gate", ("mode", "gate", None, None)),
    ("J3", "JUDGE each", ("mode", "each", None, None)),
    ("J4", "JUDGE off session", ("mode", "off", None, None)),
    ("J5", "JUDGE default", ("mode", None, None, None)),
    ("J6", "JUDGE round CON-20261118-01 gate", ("round", None, "CON-20261118-01", "gate")),
    ("J7", "JUDGE round CON-20261118-01 step", ("round", None, "CON-20261118-01", "step")),
    ("J8", "JUDGE each\nthen fix the tests", ("mode", "each", None, None)),
    ("J9", "  \t JUDGE off", ("mode", "off", None, None)),
    ("J10", "JUDGE off\r\n", ("mode", "off", None, None)),
    ("J11", "judge off", None),
    ("J12", "JUDGE  off", None),
    ("J13", "JUDGE off please", None),
    ("J14", "JUDGE off session now", None),
    ("J15", "JUDGE default session", None),
    ("J16", "JUDGE round CON-1 gate", None),
    ("J17", "JUDGE round CON-20261118-01 both", None),
    ("J18", "JUDGE round CON-20261118-01", None),
    ("J19", "Please JUDGE off", None),
    ("J20", '<agent-message from="x">\nJUDGE off', None),
    ("J21", "", None),
    ("J22", "JUDGE", None),
    ("J23", "JUDGE round CON-20261118-01 gate session", None),
    ("J24", "JUDGE off\t", None),
]


class LineTests(unittest.TestCase):
    def test_each_prompt_of_j1_to_j24(self):
        module = load_module("judge_line_under_test", ROOT / "hooks" / "judge_line.py")
        found = {}

        def refuse(*_args, **_kwargs):
            raise AssertionError("the line reader touched a file or a process")

        with mock.patch("builtins.open", side_effect=refuse) as opened, mock.patch("os.open", side_effect=refuse) as os_opened, \
                mock.patch("subprocess.run", side_effect=refuse) as ran:
            for row_id, prompt, _expected in LINE_ROWS:
                found[row_id] = module.judge_line(prompt)
            found["number"], found["null"] = module.judge_line(5), module.judge_line(None)
        self.assertEqual((opened.call_count, os_opened.call_count, ran.call_count), (0, 0, 0))
        self.assertEqual(len(LINE_ROWS), 24)
        for row_id, prompt, expected in LINE_ROWS:
            with self.subTest(row=row_id, prompt=prompt):
                self.assertEqual(found[row_id], expected)
        self.assertEqual((found["number"], found["null"]), (None, None))


# ===================================================================================== HookTests

OUT = '{"hookSpecificOutput": {"additionalContext": "%s", "hookEventName": "UserPromptSubmit"}}\n'
MODE_OFF = "readback: processed judge-mode request for off; duplicate delivery changes nothing"
MODE_EACH = "readback: processed judge-mode request for each; duplicate delivery changes nothing"
MODE_DEFAULT = "readback: processed judge-mode request for default; duplicate delivery changes nothing"
ROUND_GATE = "readback: processed judge-round request for CON-20261118-01 gate; a duplicate delivery grants nothing more"
ROUND_FAILED = "readback: the judge round was not stored: INVALID"
MISSING = "readback: the judge line was not stored: MISSING_EVENT_ID"
STORED_OFF = "readback: judge mode off for this session (owner's judge line of {AT})"
NO_OUTPUT = None


def say(text):
    return None if text is None else (OUT % text).encode("ascii")


def U(prompt, **extra):
    event = {"hook_event_name": "UserPromptSubmit", "session_id": SID, "cwd": "{CWD}", "prompt": prompt,
             "prompt_id": "e1"}
    event.update(extra)
    return event


def S(name, **extra):
    event = {"hook_event_name": name, "session_id": SID, "cwd": "{CWD}"}
    event.update(extra)
    return event


OFF_STATE = ("off", "prompt:p0", [], ["prompt:p0"])
FULL_STATE = ("each", "prompt:p0", [("prompt:a0", "CON-20260101-01", "step")], ["prompt:p0", "prompt:a0"])
PREP_FULL = [("mode", "each", "prompt:p0"), ("auth", "prompt:a0", "CON-20260101-01", "step")]

# (id, steps). A step is one of: ("mode", mode, event id), ("auth", event id, lineage, tier), ("file", bytes),
# ("settings", object), ("run", event, expected text), ("raw", bytes, expected text), ("par", event, count, expected
# text), ("consume_first",), ("view", expected), ("file_equals", bytes), ("auth_target", lineage, tier), ("empty",).
H_ROWS = [
    ("H1", [("run", U("JUDGE off"), MODE_OFF), ("view", ("off", "prompt:e1", [], ["prompt:e1"]))]),
    ("H2", PREP_FULL + [("run", U("JUDGE each session", prompt_id="e2"), MODE_EACH),
                        ("view", ("each", "prompt:e2", [("prompt:a0", "CON-20260101-01", "step")],
                                  ["prompt:p0", "prompt:a0", "prompt:e2"]))]),
    ("H3", [("mode", "off", "prompt:p0"), ("run", U("JUDGE default"), MODE_DEFAULT),
            ("view", (None, "prompt:e1", [], ["prompt:p0", "prompt:e1"]))]),
    ("H4", [("settings", {"schema_version": 1, "commit_check": "off"}), ("run", U(ROUND), ROUND_GATE),
            ("view", (None, None, [("prompt:e1", "CON-20261118-01", "gate")], ["prompt:e1"])),
            ("auth_target", "CON-20261118-01", "gate")]),
    ("H5", [("settings", {"schema_version": 1}), ("run", U(ROUND), ROUND_GATE), ("run", U(ROUND), ROUND_GATE),
            ("view", (None, None, [("prompt:e1", "CON-20261118-01", "gate")], ["prompt:e1"]))]),
    ("H6", [("run", U(ROUND, cwd="{HOME}/missing"), ROUND_FAILED), ("view", "absent"), ("nologbook",)]),
    ("H7", [("mode", "off", "prompt:p0"), ("snapshot",), ("run", U("fix the tests"), STORED_OFF), ("unchanged",), ("view", OFF_STATE)]),
    ("H8", [("run", U("fix the tests"), NO_OUTPUT), ("view", "absent"), ("nologbook",)]),
    ("H9", [("run", U('<agent-message from="x">\nJUDGE off'), NO_OUTPUT), ("view", "absent"), ("nologbook",)]),
    ("H10", [("run", {"hook_event_name": "UserPromptSubmit", "prompt": "JUDGE off", "prompt_id": "e1"}, NO_OUTPUT),
             ("run", U("JUDGE off", session_id="../x"), NO_OUTPUT),
             ("run", U("JUDGE off", session_id="a/b"), NO_OUTPUT), ("nologbook",)]),
    ("H11", PREP_FULL + [("run", S("SessionStart", source="resume"), NO_OUTPUT), ("view", "absent")]),
    ("H12", PREP_FULL + [("run", S("SessionStart", source="fork"), NO_OUTPUT), ("view", "absent")]
     + PREP_FULL + [("snapshot",), ("run", S("SessionStart", source="startup"), NO_OUTPUT), ("unchanged",), ("view", FULL_STATE)]),
    ("H13", PREP_FULL + [("run", S("SessionEnd", reason="other"), NO_OUTPUT), ("view", "absent")]),
    ("H14", [("raw", b"this is not json", NO_OUTPUT), ("raw", b'["JUDGE off"]', NO_OUTPUT),
             ("run", U("JUDGE off", hook_event_name="PostToolUse"), NO_OUTPUT), ("view", "absent"), ("nologbook",)]),
    ("H15", [("file", b'{"not": "a state"}\n'), ("run", U("JUDGE off"), NO_OUTPUT),
             ("file_equals", b'{"not": "a state"}\n')]),
    ("H16", [("run", {"hook_event_name": "UserPromptSubmit", "session_id": SID, "cwd": "{CWD}", "prompt": "JUDGE off",
                      "turn_id": "t1"}, MODE_OFF),
             ("view", ("off", "turn:t1", [], ["turn:t1"])),
             ("run", {"hook_event_name": "UserPromptSubmit", "session_id": SID, "cwd": "{CWD}", "prompt": "JUDGE each"},
              MISSING),
             ("view", ("off", "turn:t1", [], ["turn:t1"]))]),
    ("H17", [("run", U("JUDGE off", agent_id="a1"), NO_OUTPUT), ("view", "absent"), ("nologbook",)]),
    ("H18", [("run", U("JUDGE off", agent_type="worker"), NO_OUTPUT), ("view", "absent"), ("nologbook",)]),
    ("H19", [("mode", "off", "prompt:p0"), ("snapshot",), ("run", U("JUDGE default", agent_id="a1"), NO_OUTPUT), ("unchanged",), ("view", OFF_STATE)]),
    ("H20", [("run", U("JUDGE off", prompt_id="A"), MODE_OFF), ("run", U("JUDGE each", prompt_id="B"), MODE_EACH),
             ("run", U("JUDGE off", prompt_id="A"), MODE_OFF),
             ("view", ("each", "prompt:B", [], ["prompt:A", "prompt:B"]))]),
    ("H21", [("settings", {"schema_version": 1}), ("run", U(ROUND), ROUND_GATE), ("consume_first",),
             ("view", (None, None, [], ["prompt:e1"])), ("run", U(ROUND), ROUND_GATE),
             ("view", (None, None, [], ["prompt:e1"]))]),
    ("H22", [("settings", {"schema_version": 1}), ("run", U(ROUND, prompt_id="e1"), ROUND_GATE),
             ("run", U(ROUND, prompt_id="e2"), ROUND_GATE),
             ("view", (None, None, [("prompt:e1", "CON-20261118-01", "gate"), ("prompt:e2", "CON-20261118-01", "gate")],
                       ["prompt:e1", "prompt:e2"]))]),
    ("H23", [("settings", {"schema_version": 1}), ("par", U(ROUND), 8, ROUND_GATE),
             ("view", (None, None, [("prompt:e1", "CON-20261118-01", "gate")], ["prompt:e1"]))]),
    ("H24", PREP_FULL + [("snapshot",), ("run", S("SessionStart", source="compact"), NO_OUTPUT), ("unchanged",), ("view", FULL_STATE),
                         ("run", S("SessionStart", source="clear"), NO_OUTPUT), ("unchanged",), ("view", FULL_STATE)]),
    ("H25", PREP_FULL + [("run", S("SessionStart", source="resume"), NO_OUTPUT), ("view", "absent"),
                         ("run", S("SessionStart", source="resume"), NO_OUTPUT), ("view", "absent")]),
    ("H26", [("run", U(5), NO_OUTPUT), ("run", U(None), NO_OUTPUT), ("view", "absent"), ("nologbook",),
             ("mode", "off", "prompt:p0"), ("snapshot",), ("run", U(5), STORED_OFF), ("run", U(None), STORED_OFF), ("unchanged",), ("view", OFF_STATE)]),
]

TRACE_SESSION = b'''import importlib.util, os
_spec = importlib.util.spec_from_file_location("real_session", os.path.join(os.environ["REAL_SCRIPTS"], "readback_session.py"))
real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real)
safe_id = real.safe_id
def read(*args, **kwargs):
    open(os.environ["TRACE"], "a").write("read\\n")
    return real.read(*args, **kwargs)
def __getattr__(name):
    open(os.environ["TRACE"], "a").write(name + "\\n")
    return getattr(real, name)
'''
TRACE_SETTINGS = b'''import importlib.util, os
_spec = importlib.util.spec_from_file_location("real_settings", os.path.join(os.environ["REAL_SCRIPTS"], "readback_settings.py"))
real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real)
def resolve(*args, **kwargs):
    open(os.environ["TRACE"], "a").write("resolve\\n")
    return real.resolve(*args, **kwargs)
def load(*args, **kwargs):
    open(os.environ["TRACE"], "a").write("load\\n")
    return real.load(*args, **kwargs)
def __getattr__(name):
    return getattr(real, name)
'''


class Case:
    """One scratch HOME, project folder and session folder for one run of a row."""

    def __init__(self, test, script, host):
        self.test, self.script, self.host = test, script, host
        self.home = scratch(test)
        self.cwd = os.path.join(self.home, "proj")
        self.work = os.path.join(self.home, "work")
        os.makedirs(self.cwd)
        os.makedirs(self.work)
        self.base = os.path.join(self.home, "logbook", ".readback", "sessions")
        self.env = environment(self.home)

    def fill(self, value):
        if isinstance(value, str):
            return value.replace("{CWD}", self.cwd).replace("{HOME}", self.home)
        if isinstance(value, dict):
            return {key: self.fill(item) for key, item in value.items()}
        return value

    def state_path(self):
        return os.path.join(self.base, self.host + "-" + SID + ".json")

    def view(self):
        kind, state = rs.read(self.host, SID, base=self.base)
        if kind != "valid":
            return kind if kind == "absent" else (kind, state)
        event = state["mode_event"]
        return (state["mode"], event["id"] if event else None,
                [(item["event_id"], item["lineage"], item["tier"]) for item in state["authorizations"]],
                state["processed_events"])

    def run(self, event=None, raw=None):
        result = run_hook(self.script, self.host, self.fill(event) if raw is None else None, raw, self.env, self.work)
        self.test.assertEqual((result.stderr, result.returncode), (b"", 0))
        return result.stdout

    def expected(self, text):
        if text is None:
            return b""
        if "{AT}" in text:
            kind, state = rs.read(self.host, SID, base=self.base)
            text = text.replace("{AT}", state["mode_event"]["at"])
        return say(text)

    def times_are_utc(self):
        kind, state = rs.read(self.host, SID, base=self.base)
        if kind != "valid":
            return
        stamps = [state["mode_event"]["at"]] if state["mode_event"] else []
        stamps += [item["at"] for item in state["authorizations"]]
        for stamp in stamps:
            self.test.assertRegex(stamp, AT_FORM)
            if stamp != FIXED_AT:
                moment = calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
                self.test.assertLess(abs(time.time() - moment), 300)

    def step(self, step):
        name, test = step[0], self.test
        if name == "mode":
            rs.set_mode(self.host, SID, step[1], step[2], FIXED_AT, base=self.base)
        elif name == "auth":
            rs.authorize(self.host, SID, step[1], FIXED_AT, "/home/user/h", "/home/user/r", step[2], step[3], "d" * 64,
                         base=self.base)
        elif name == "file":
            os.makedirs(self.base, exist_ok=True)
            Path(self.state_path()).write_bytes(step[1])
        elif name == "settings":
            home = os.path.join(self.home, "logbook", "proj")
            os.makedirs(home, exist_ok=True)
            Path(home, "settings.json").write_text(json.dumps(step[1]), encoding="utf-8")
        elif name == "run":
            test.assertEqual(self.run(step[1]), self.expected(step[2]))
        elif name == "raw":
            test.assertEqual(self.run(raw=step[1]), b"")
        elif name == "par":
            event = self.fill(step[1])
            with concurrent.futures.ThreadPoolExecutor(max_workers=step[2]) as pool:
                results = list(pool.map(lambda _i: run_hook(self.script, self.host, event, None, self.env, self.work),
                                        range(step[2])))
            for result in results:
                test.assertEqual((result.stdout, result.stderr, result.returncode), (say(step[3]), b"", 0))
        elif name == "consume_first":
            kind, state = rs.read(self.host, SID, base=self.base)
            item = state["authorizations"][0]
            got = rs.consume(self.host, SID, *(item[key] for key in rs.TARGET_FIELDS), base=self.base)
            test.assertEqual(got, item)
        elif name == "view":
            test.assertEqual(self.view(), step[1])
            self.times_are_utc()
        elif name == "file_equals":
            test.assertEqual(Path(self.state_path()).read_bytes(), step[1])
        elif name == "auth_target":
            kind, state = rs.read(self.host, SID, base=self.base)
            item = state["authorizations"][0]
            home = os.path.join(self.home, "logbook", "proj")
            test.assertEqual(item["project_home"], home)
            test.assertEqual(item["repository"], self.cwd)
            test.assertEqual(item["lineage"], step[1])
            test.assertEqual(item["tier"], step[2])
            expected = copy.deepcopy(rsettings.DEFAULTS)
            expected["commit_check"] = "off"
            canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            test.assertEqual(item["settings_digest"], hashlib.sha256(canonical).hexdigest())
            test.assertNotEqual(item["settings_digest"], rsettings.digest(rsettings.DEFAULTS))
        elif name == "snapshot":
            self.snapshot = (rs.read(self.host, SID, base=self.base),
                             Path(self.state_path()).read_bytes() if os.path.exists(self.state_path()) else None)
        elif name == "unchanged":
            now = (rs.read(self.host, SID, base=self.base),
                   Path(self.state_path()).read_bytes() if os.path.exists(self.state_path()) else None)
            test.assertEqual(now, self.snapshot)
        elif name == "nologbook":
            test.assertFalse(os.path.exists(os.path.join(self.home, "logbook")))
        elif name == "empty":
            names = [n for n in os.listdir(self.base) if not n.endswith(".lock")] if os.path.isdir(self.base) else []
            test.assertEqual(names, [])
        else:
            raise AssertionError(name)


class HookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = os.path.realpath(tempfile.mkdtemp(prefix="jl-hook-"))
        cls.addClassCleanup(tco.remove, cls.folder)
        cls.hooks = {kind: make_layout(kind, os.path.join(cls.folder, kind)) / "judge_line.py" for kind in LAYOUTS}
        stubs = {"readback_session.py": TRACE_SESSION, "readback_settings.py": TRACE_SETTINGS}
        cls.traced = {kind: make_layout(kind, os.path.join(cls.folder, "t-" + kind), stubs=stubs) / "judge_line.py"
                      for kind in LAYOUTS}

    def test_events_h1_to_h26(self):
        self.assertEqual([row[0] for row in H_ROWS], ["H%d" % number for number in range(1, 27)])
        for kind in LAYOUTS:
            for host in HOSTS:
                for row_id, steps in H_ROWS:
                    with self.subTest(layout=kind, host=host, row=row_id):
                        case = Case(self, self.hooks[kind], host)
                        for step in steps:
                            case.step(step)

    def test_no_bytecode_is_written(self):
        case = Case(self, self.hooks["plugin"], "claude")
        case.step(("settings", {"schema_version": 1}))
        case.step(("run", U(ROUND), ROUND_GATE))
        for kind in LAYOUTS:
            self.assertEqual(list(self.hooks[kind].parent.parent.rglob("__pycache__")), [])
            self.assertEqual(list(self.hooks[kind].parent.rglob("*.pyc")), [])

    def test_extra_inputs(self):
        for kind in LAYOUTS:
            for argument in ([], ["other"]):
                with self.subTest(layout=kind, argument=argument):
                    case = Case(self, self.hooks[kind], "claude")
                    result = subprocess.run([sys.executable, str(self.hooks[kind])] + argument,
                                            input=json.dumps(case.fill(U("JUDGE off"))).encode(), capture_output=True,
                                            env=case.env, cwd=case.work, timeout=60)
                    self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))
                    case.step(("view", "absent"))
                    case.step(("nologbook",))
            for host in HOSTS:
                with self.subTest(layout=kind, host=host, row="null mode"):
                    case = Case(self, self.hooks[kind], host)
                    case.step(("mode", None, "prompt:p0"))
                    case.step(("run", U("fix the tests"), NO_OUTPUT))
                    case.step(("view", (None, "prompt:p0", [], ["prompt:p0"])))
                with self.subTest(layout=kind, host=host, row="unknown event with a clearing source"):
                    case = Case(self, self.hooks[kind], host)
                    for step in PREP_FULL + [("snapshot",)]:
                        case.step(step)
                    case.step(("run", S("PostToolUse", source="resume"), NO_OUTPUT))
                    case.step(("run", S("Notification", source="fork"), NO_OUTPUT))
                    case.step(("unchanged",))
                    case.step(("view", FULL_STATE))
                for label, event in (("no cwd", {"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": ROUND,
                                                 "prompt_id": "e1"}),
                                     ("number cwd", U(ROUND, cwd=7))):
                    with self.subTest(layout=kind, host=host, row=label):
                        case = Case(self, self.hooks[kind], host)
                        case.step(("run", event, ROUND_GATE))
                        item = rs.read(host, SID, base=case.base)[1]["authorizations"][0]
                        self.assertEqual((item["project_home"], item["repository"]),
                                         (os.path.join(case.home, "logbook", "work"), case.work))

    def test_h7_and_h8_read_the_state_once_and_resolve_nothing(self):
        for kind in LAYOUTS:
            for host in HOSTS:
                for row_id, prep, text in (("H7", [("mode", "off", "prompt:p0")], STORED_OFF), ("H8", [], None)):
                    with self.subTest(layout=kind, host=host, row=row_id):
                        case = Case(self, self.traced[kind], host)
                        trace = os.path.join(case.home, "trace.log")
                        case.env.update(TRACE=trace, REAL_SCRIPTS=str(ROOT / "scripts"))
                        for step in prep:
                            case.step(step)
                        case.step(("run", U("fix the tests"), text))
                        self.assertEqual(Path(trace).read_text().split(), ["read"])
                        self.assertEqual(case.view(), OFF_STATE if prep else "absent")

    def test_the_trace_sees_a_round_resolve(self):
        # The trace layout is able to see a resolution: a round line shows resolve, load and authorize.
        case = Case(self, self.traced["plugin"], "claude")
        trace = os.path.join(case.home, "trace.log")
        case.env.update(TRACE=trace, REAL_SCRIPTS=str(ROOT / "scripts"))
        case.step(("run", U(ROUND), ROUND_GATE))
        calls = Path(trace).read_text().split()
        self.assertIn("resolve", calls)
        self.assertIn("load", calls)

    def test_the_session_variables_of_the_environment_are_not_used(self):
        case = Case(self, self.hooks["plugin"], "claude")
        case.step(("run", U("JUDGE off"), MODE_OFF))
        self.assertEqual(sorted(n for n in os.listdir(case.base) if n.endswith(".json")), ["claude-s1.json"])


# ===================================================================================== GuardTests

DENY_REASON = ("readback blocks %s. Only the owner changes the judge settings and the session state. "
               "Tell the owner the command; the owner can run it in a terminal.")
G1 = "python3 scripts/readback_settings.py --host claude set judge.mode off"
G4 = "python3 scripts/readback_settings.py --host claude migrate-teammates --write"
G8_INNER = "python3 scripts/readback_settings.py --host codex set commit_check off"
G12 = "git reset --hard && python3 scripts/readback_settings.py --host claude set judge.mode off"
EXISTING_REASON = ("readback blocks `git reset --hard`. It can discard changes that another person or agent made. "
                   "Tell the owner the command and the reason. The owner can run it outside the agent.")
# (id, command, environment additions, event additions, expected deny reason or None)
G_ROWS = [
    ("G1", G1, {}, {}, DENY_REASON % G1),
    ("G2", "python3 -B /home/user/readback/scripts/readback_settings.py --host claude set judge.mode off", {}, {},
     DENY_REASON % "python3 -B /home/user/readback/scripts/readback_settings.py --host claude set judge.mode off"),
    ("G3", "python3 scripts/readback_settings.py --host claude show", {}, {}, None),
    ("G4", G4, {}, {}, DENY_REASON % G4),
    ("G5", "python3 scripts/readback_settings.py --host claude migrate-teammates", {}, {}, None),
    ("G6", "cat ~/logbook/.readback/sessions/claude-x.json", {}, {},
     DENY_REASON % "cat ~/logbook/.readback/sessions/claude-x.json"),
    ("G7", 'echo x > "$HOME/logbook/.readback/sessions/claude-x.json"', {}, {},
     DENY_REASON % "$HOME/logbook/.readback/sessions/claude-x.json"),
    ("G8", "bash -c 'python3 scripts/readback_settings.py --host codex set commit_check off'", {}, {},
     DENY_REASON % G8_INNER),
    ("G9", "ls ~/logbook/.readback", {}, {}, None),
    ("G10", "grep set scripts/readback_settings.py", {}, {}, None),
    ("G11", G1, {}, {"agent_id": "a1", "agent_type": "readback:worker"}, DENY_REASON % G1),
    ("G12", G12, {}, {}, EXISTING_REASON),
    ("G13", "echo scripts/readback_settings.py set", {}, {}, DENY_REASON % "echo scripts/readback_settings.py set"),
    ("G14", G1, {"READBACK_COMMIT_CHECK": "off"}, {}, DENY_REASON % G1),
    ("G15", "S=readback_settings.py; python3 scripts/$S --host claude set judge.mode off", {}, {}, None),
    ("G16", "python3 scripts/readback_settings.py --host claude set 'judge.mode", {}, {}, None),
]


def deny_bytes(reason):
    return (json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                "permissionDecisionReason": reason}}) + "\n").encode("ascii")


def bash_event(command, cwd=None, **extra):
    return tco.bash_event(command, cwd, **extra)


def base_layout(dest):
    """The plugin layout of the base commit: `git archive` of hooks/ and scripts/ below dest. Return the guard hook path."""
    shown = subprocess.run(["git", "-C", str(ROOT), "archive", BASE_COMMIT, "hooks", "scripts"], capture_output=True,
                           check=True, timeout=60, env=environment(str(dest)))
    with tarfile.open(fileobj=io.BytesIO(shown.stdout)) as archive:
        archive.extractall(dest, filter="data")
    return Path(dest, "hooks", "guard_git.py")


class GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = os.path.realpath(tempfile.mkdtemp(prefix="jl-guard-"))
        cls.addClassCleanup(tco.remove, cls.folder)
        cls.guards = {kind: make_layout(kind, os.path.join(cls.folder, kind)) / "guard_git.py" for kind in LAYOUTS}
        cls.guards_without_settings = {
            kind: make_layout(kind, os.path.join(cls.folder, "no-" + kind), drop=("readback_settings.py",)) / "guard_git.py"
            for kind in LAYOUTS}
        cls.base_guard = base_layout(os.path.join(cls.folder, "base"))

    def test_command_lines_g1_to_g16(self):
        self.assertEqual([row[0] for row in G_ROWS], ["G%d" % number for number in range(1, 17)])
        for kind in LAYOUTS:
            for host in HOSTS:
                for row_id, command, env_add, event_add, reason in G_ROWS:
                    with self.subTest(layout=kind, host=host, row=row_id):
                        home = scratch(self)
                        result = run_hook(self.guards[kind], host, bash_event(command, **event_add),
                                          env=environment(home, **env_add), cwd=home)
                        self.assertEqual((result.stderr, result.returncode), (b"", 0))
                        self.assertEqual(result.stdout, b"" if reason is None else deny_bytes(reason))

    def test_a_long_deny_text_is_cut_at_120_characters(self):
        command = "python3 scripts/readback_settings.py --host claude set judge.mode off " + "x" * 200
        home = scratch(self)
        result = run_hook(self.guards["plugin"], "claude", bash_event(command), env=environment(home), cwd=home)
        self.assertEqual(result.stdout, deny_bytes(DENY_REASON % command[:120]))
        self.assertEqual(len(command[:120]), 120)

    def test_no_bytecode_is_written_by_the_guard(self):
        for kind in LAYOUTS:
            self.assertEqual(list(self.guards[kind].parent.parent.rglob("__pycache__")), [])

    def test_a_deny_is_the_same_for_a_sub_agent_and_the_lead(self):
        home = scratch(self)
        lead = run_hook(self.guards["plugin"], "claude", bash_event(G1), env=environment(home), cwd=home)
        agent = run_hook(self.guards["plugin"], "claude", bash_event(G1, agent_type="readback:worker"),
                         env=environment(home), cwd=home)
        self.assertEqual(lead.stdout, agent.stdout)

    def test_the_tables_are_those_of_the_base_commit(self):
        for name in ("test_commit_observation.py",):
            shown = at_base("checks/" + name)
            self.assertEqual(hashlib.sha256((ROOT / "checks" / name).read_bytes()).hexdigest(),
                             hashlib.sha256(shown).hexdigest())
            self.assertEqual(hashlib.sha256(shown).hexdigest(), COMMIT_OBSERVATION_TESTS_SHA256)
        guard_tests = at_base("checks/test_guard_git.py").decode()
        self.assertEqual(guard_tests.count('    "git reset --hard",\n'), 1)
        self.assertEqual(len(DENIED), 51)
        self.assertEqual(len(ALLOWED), 26)
        self.assertEqual(len(tco.CONTRACT_LINES), 64)

    def test_denied_and_allowed_lines_equal_the_base_hook(self):
        empty, empty_base = scratch(self), scratch(self)
        env = environment(empty)
        oracle = {}

        def base_result(line):
            if line not in oracle:
                oracle[line] = run_hook(self.base_guard, None, bash_event(line), env=env, cwd=empty_base)
            return oracle[line]

        jobs = [(kind, host, line) for kind in LAYOUTS for host in HOSTS for line in DENIED + ALLOWED]

        def job(item):
            kind, host, line = item
            return run_hook(self.guards[kind], host, bash_event(line), env=env, cwd=empty), base_result(line)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(job, jobs))
        for (kind, host, line), (new, old) in zip(jobs, results):
            with self.subTest(layout=kind, host=host, line=line):
                self.assertEqual(tco.differences(new, old), [])
        self.assertIn(b"permissionDecision", results[0][0].stdout)

    def test_lines_c1_to_c58_equal_the_base_hook(self):
        env_home = scratch(self)
        env = environment(env_home)
        jobs = [(kind, host, row) for kind in LAYOUTS for host in HOSTS for row in tco.CONTRACT_LINES]

        def job(item):
            kind, host, row = item
            text = tco.render(row[1], tco.HOOK_VALUES)
            new_repo, old_repo = tco.commit_repo(self, name="r"), tco.commit_repo(self, name="r")
            new = run_hook(self.guards[kind], host, bash_event(text, new_repo.base), env=env, cwd=new_repo.base)
            old = run_hook(self.base_guard, None, bash_event(text, old_repo.base), env=env, cwd=old_repo.base)
            return new, old, len(new_repo.lines()), len(old_repo.lines())

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(job, jobs))
        for (kind, host, row), (new, old, new_count, old_count) in zip(jobs, results):
            with self.subTest(layout=kind, host=host, row=row[0]):
                self.assertEqual(tco.differences(new, old), [])
                self.assertEqual((new.stdout, new.stderr, new.returncode), (b"", b"", 0))
                self.assertEqual(new_count, old_count)

    def commit_case(self, guard, host, other_env, setup, host_argument=True, flip=None):
        """Run one commit call in a repository with readback/reviews. Return the number of lines after the run.

        With `flip`, the fixture changes afterwards and the call runs again; the result is then (first, second).
        """
        home = scratch(self)
        repo = tco.commit_repo(self, name="proj")
        project = os.path.join(home, "logbook", "proj")
        os.makedirs(project)
        setup(home, project)
        env = environment(home, **other_env)
        result = run_hook(guard, host if host_argument else None, bash_event("git commit -m x", repo.base), env=env,
                          cwd=repo.base)
        self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))
        for line in repo.lines():
            self.assertEqual(line["eligibility"], "UNVERIFIED")
        if flip is None:
            return len(repo.lines())
        first = len(repo.lines())
        flip(home, project)
        result = run_hook(guard, host if host_argument else None, bash_event("git commit -m x", repo.base), env=env,
                          cwd=repo.base)
        self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))
        for line in repo.lines():
            self.assertEqual(line["eligibility"], "UNVERIFIED")
        return first, len(repo.lines())

    def test_the_commit_check_switch(self):
        def settings(value):
            return lambda home, project: Path(project, "settings.json").write_text(
                json.dumps({"schema_version": 1, "commit_check": value}), encoding="utf-8")

        def broken_registry(home, project):
            settings("off")(home, project)
            os.makedirs(os.path.join(home, "logbook", ".readback"))
            Path(home, "logbook", ".readback", "projects.json").write_text("{", encoding="utf-8")

        def broken_settings(home, project):
            Path(project, "settings.json").write_text("{", encoding="utf-8")

        cases = [
            ("off", "plugin", settings("off"), 0), ("off", "portable", settings("off"), 0),
            ("observe", "plugin", settings("observe"), 1), ("no file", "plugin", lambda h, p: None, 1),
            ("import fails", "no-plugin", settings("off"), 1), ("import fails", "no-portable", settings("off"), 1),
            ("resolution fails", "plugin", broken_registry, 1), ("load fails", "plugin", broken_settings, 1),
            ("no file", "portable", lambda h, p: None, 1), ("observe", "portable", settings("observe"), 1),
            ("resolution fails", "portable", broken_registry, 1), ("load fails", "portable", broken_settings, 1),
        ]
        for host, other in (("claude", {"CODEX_SESSION_ID": "other", "CODEX_HOME": "/home/user/.codex"}),
                            ("codex", {"CLAUDE_CODE_SESSION_ID": "other", "CLAUDE_PROJECT_DIR": "/home/user/p"})):
            for label, kind, setup, count in cases:
                guard = (self.guards_without_settings[kind[3:]] if kind.startswith("no-") else self.guards[kind])
                with self.subTest(host=host, case=label, layout=kind):
                    if count == 0:
                        self.assertEqual(self.commit_case(guard, host, other, setup, flip=settings("observe")), (0, 1))
                    else:
                        self.assertEqual(self.commit_case(guard, host, other, setup), count)

    def test_without_a_host_argument_the_switch_is_skipped(self):
        def off(home, project):
            Path(project, "settings.json").write_text(json.dumps({"schema_version": 1, "commit_check": "off"}),
                                                      encoding="utf-8")

        for kind in LAYOUTS:
            with self.subTest(layout=kind):
                self.assertEqual(self.commit_case(self.guards[kind], None, {}, off, host_argument=False), 1)

    def test_the_environment_switch_still_stops_the_observation(self):
        home = scratch(self)
        repo = tco.commit_repo(self, name="proj")
        env = environment(home, READBACK_COMMIT_CHECK="off")
        result = run_hook(self.guards["plugin"], "claude", bash_event("git commit -m x", repo.base), env=env, cwd=repo.base)
        self.assertEqual((result.stdout, result.stderr, result.returncode, repo.lines()), (b"", b"", 0, []))


# ===================================================================================== ConfigTests

CLAUDE_JUDGE = 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/judge_line.py" claude'
CLAUDE_GUARD = 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guard_git.py" claude'
CODEX_JUDGE = 'python3 "$HOME/.agents/skills/readback/scripts/judge_line.py" codex'
CODEX_GUARD = 'python3 "$HOME/.agents/skills/readback/scripts/guard_git.py" codex'


def pinned():
    folder = os.path.realpath(tempfile.mkdtemp(prefix="jl-env-"))
    return environment(folder), folder


def at_base(path):
    env, folder = pinned()
    try:
        return subprocess.run(["git", "-C", str(ROOT), "show", BASE_COMMIT + ":" + path], capture_output=True, check=True,
                              timeout=60, env=env).stdout
    finally:
        shutil.rmtree(folder, ignore_errors=True)


class ConfigTests(unittest.TestCase):
    def test_the_plugin_file(self):
        config = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(list(config), ["hooks"])
        self.assertEqual(list(config["hooks"]), ["PreToolUse", "UserPromptSubmit", "SessionStart", "SessionEnd"])
        for name in ("UserPromptSubmit", "SessionStart", "SessionEnd"):
            self.assertEqual(config["hooks"][name], [{"hooks": [{"type": "command", "command": CLAUDE_JUDGE, "timeout": 10}]}])
        old = json.loads(at_base("hooks/hooks.json"))["hooks"]["PreToolUse"]
        old[0]["hooks"][0]["command"] += " claude"
        self.assertEqual(config["hooks"]["PreToolUse"], old)
        self.assertEqual(config["hooks"]["PreToolUse"][0]["hooks"][0]["command"], CLAUDE_GUARD)

    def test_the_codex_file(self):
        config = json.loads((ROOT / "portable" / "codex" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(list(config), ["hooks"])
        self.assertEqual(list(config["hooks"]), ["PreToolUse", "UserPromptSubmit", "SessionStart", "SessionEnd"])
        for name in ("UserPromptSubmit", "SessionStart", "SessionEnd"):
            self.assertEqual(config["hooks"][name], [{"hooks": [{"type": "command", "command": CODEX_JUDGE, "timeout": 10,
                                                                 "statusMessage": "readback: reading the judge line"}]}])
        old = json.loads(at_base("portable/codex/hooks.json"))["hooks"]["PreToolUse"]
        old[0]["hooks"][0]["command"] += " codex"
        self.assertEqual(config["hooks"]["PreToolUse"], old)
        self.assertEqual(config["hooks"]["PreToolUse"][0]["hooks"][0]["command"], CODEX_GUARD)

    def test_the_portable_copies_equal_their_sources(self):
        portable = ROOT / "portable" / "skills" / "readback" / "scripts"
        self.assertEqual((portable / "judge_line.py").read_bytes(), (ROOT / "hooks" / "judge_line.py").read_bytes())
        self.assertEqual((portable / "guard_git.py").read_bytes(), (ROOT / "hooks" / "guard_git.py").read_bytes())
        self.assertEqual((portable / "commit_observation.py").read_bytes(), (ROOT / "scripts" / "commit_observation.py").read_bytes())

    def test_sync_check_exits_zero(self):
        env, folder = pinned()
        self.addCleanup(shutil.rmtree, folder, True)
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "sync.py"), "--check"], capture_output=True,
                                timeout=60, env=env)
        self.assertEqual((result.returncode, result.stderr), (0, b""))


if __name__ == "__main__":
    unittest.main()
