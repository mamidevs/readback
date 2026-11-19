#!/usr/bin/env python3
"""Offline checks for the modes of scripts/outside_judge.py: tier, judge mode, route, deadline, validation gate.

Run one class: python3 -B checks/test_judge_modes.py ModeTests
Classes: ModeTests (M1 to M18 and outcome), ValidationGateTests (12 cells), RouteTests (R1 to R17, T1 to T5),
RecordTests (the call object, the records of off and skipped, imports, the portable copy).

Every helper run sets HOME to a temporary folder and removes both session variables unless a row sets one.
Fake providers replace both CLIs. No model call and no network run.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
HELPER = SCRIPTS / "outside_judge.py"
PORTABLE = ROOT / "portable" / "skills" / "readback" / "scripts" / "outside_judge.py"
BASE_SHA = "f4a4ed52e1caf68b787c2df3a0be636cfbdc8185"
sys.path.insert(0, str(SCRIPTS))
sys.dont_write_bytecode = True
import outside_judge  # noqa: E402
import readback_session  # noqa: E402
import readback_settings  # noqa: E402
import review_receipt  # noqa: E402
import validate_handoff  # noqa: E402

REAL_GIT = shutil.which("git")
BUNDLE_FILES = ("outside_judge.py", "review_receipt.py", "review_capture.py", "validate_handoff.py",
                "readback_settings.py", "readback_session.py", "project_registry.py", "judge_rounds.py",
                "review_redaction.py", "review_retention.py")
AT = "2026-11-18T10:00:00Z"
STARTED = "see the complete answer below"

FAKE_CLI = r'''
import json, os, pathlib, sys
provider = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
prompt = sys.stdin.buffer.read()
with open(os.environ["FAKE_RECORD"], "a") as record:
    record.write(json.dumps({"provider": provider, "args": args, "prompt_hex": prompt.hex()}) + "\n")
answer = os.environ.get("FAKE_ANSWER", "PASS Evidence supports the handoff.\nKeep this complete finding.\n")
if provider == "codex":
    pathlib.Path(args[args.index("-o") + 1]).write_text(answer)
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": answer,
                      "modelUsage": json.loads(os.environ.get("FAKE_USAGE", '{"claude-fable-5-2": {}}'))}))
'''

CONTRACT_TRUE = ("CON-20261117-01  Do the thing.\nACC-1  The tool shall work.   Check: sh a.sh\n"
                 "ACC-2  The tool shall pass.   Check: sh b.sh\nOut of scope: everything else\n")
HANDOFF_TRUE = ("id        HND-20261117-01\nfrom      worker\ndate      2026-11-17\nstate     DONE\n"
                "readback  task: x | limits: y | done when: ACC-1, ACC-2\nsummary   Done.\nchanges   none\n"
                "commit    none\nchecks    sh a.sh | exit 0 | ok\n          sh b.sh | exit 0 | ok\n"
                "contract  ACC-1 VERIFIED | ACC-2 VERIFIED\nopen      -\nissues    -\nnext      Accept.\n")
HANDOFF_FALSE = HANDOFF_TRUE.replace("ACC-2 VERIFIED", "ACC-2 UNVERIFIED")
HANDOFF_UNPARSED = "{broken"



def model_args(route):
    """The model and the effort in the captured command line of a fake provider call."""
    args = route["args"]
    if route["provider"] == "codex":
        effort = [item for item in args if item.startswith("model_reasoning_effort=")]
        return args[args.index("-m") + 1], json.loads(effort[0].split("=", 1)[1])
    return args[args.index("--model") + 1], args[args.index("--effort") + 1]


ROUTES_FILE = {"judge": {"mode": "each", "routes": {
    "claude": {"step": {"model": "claude-sonnet-5-5", "effort": "low"},
               "gate": {"model": "claude-opus-5-5", "effort": "medium"}},
    "codex": {"step": {"model": "gpt-6.2-sol", "effort": "medium"},
              "gate": {"model": "gpt-6.2-astra", "effort": "xhigh"}}}}}


class JudgeCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="readback-modes-")
        self.addCleanup(temp.cleanup)
        self.base = Path(os.path.realpath(temp.name))
        for name in ("bin", "gitbin", "home", "tmp", "inputs"):
            (self.base / name).mkdir()
        (self.base / "gitbin" / "git").symlink_to(REAL_GIT)
        for provider in ("claude", "codex"):
            fake = self.base / "bin" / provider
            fake.write_text("#!" + sys.executable + "\n" + FAKE_CLI)
            fake.chmod(0o755)
        self.home = self.base / "home"
        self.project_home = self.home / "logbook" / "repo"
        self.sessions = self.home / "logbook" / ".readback" / "sessions"
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.record = self.base / "calls.jsonl"
        self.git("init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("alpha\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        self.common = self.repo / ".git"
        self.contract = self.base / "inputs" / "contract.md"
        self.handoff = self.base / "inputs" / "handoff.md"
        self.put(CONTRACT_TRUE, HANDOFF_TRUE)

    # ---- tools
    def git(self, *args):
        env = {"PATH": str(self.base / "gitbin"), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": "f@example.invalid",
               "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": "f@example.invalid"}
        done = subprocess.run(["git", "-C", str(self.repo), *args], env=env, capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def put(self, contract, handoff):
        self.contract.write_text(contract)
        self.handoff.write_text(handoff)

    def base_env(self, extra=None):
        env = {"HOME": str(self.home), "PATH": str(self.base / "bin") + os.pathsep + str(self.base / "gitbin"),
               "TMPDIR": str(self.base / "tmp"), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "FAKE_RECORD": str(self.record)}
        env.update(extra or {})
        return env

    def run_helper(self, host="claude", tier=None, env=None, helper=HELPER, args=None, repo=None, command=None):
        if self.record.exists():
            self.record.unlink()
        argv = command or [sys.executable, "-B", str(helper)]
        if host is not None:
            argv += ["--host", host]
        if tier is not None:
            argv += ["--tier", tier]
        argv += [str(self.contract), str(self.handoff), str(repo or self.repo)] if args is None else args
        done = subprocess.run(argv, env=self.base_env(env), capture_output=True, timeout=90)
        out = done.stdout.decode("utf-8")
        calls = []
        if self.record.exists():
            for line in self.record.read_text().splitlines():
                item = json.loads(line)
                item["prompt"] = bytes.fromhex(item.pop("prompt_hex"))
                calls.append(item)
        lines = out.split("\n")
        parts = lines[0].split(" | ", 3)
        return types.SimpleNamespace(code=done.returncode, out=out, err=done.stderr.decode("utf-8"), lines=lines,
                                     calls=calls, word=parts[0].split()[1] if parts[0].startswith("JUDGE") else None,
                                     note=parts[3] if len(parts) == 4 else None, parts=parts)

    def write_settings(self, obj):
        self.project_home.mkdir(parents=True, exist_ok=True)
        (self.project_home / "settings.json").write_text(json.dumps(dict({"schema_version": 1}, **obj)))

    def write_session(self, host, sid, *modes):
        """One mode event for each mode, the first at 2026-11-18T10:00:00Z. A null mode clears the mode."""
        for number, mode in enumerate(modes, 1):
            readback_session.set_mode(host, sid, mode, "ev-%d" % number, "2026-11-18T10:00:0%dZ" % (number - 1),
                                      base=str(self.sessions))

    def write_bad_session(self, host, sid):
        self.sessions.mkdir(parents=True, exist_ok=True)
        (self.sessions / (host + "-" + sid + ".json")).write_text("not json")

    def where(self):
        return "repo@" + self.git("rev-parse", "--short", "HEAD").strip()

    def records(self, kind):
        found = []
        root = self.common / "readback"
        if kind == "no-review":
            folder = root / "no-review"
            return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []
        for path in sorted((root / "reviews").glob("*/*." + kind + ".json")):
            found.append(json.loads(path.read_text()))
        return found

    def expected_call(self, host, sid, tier, mode, mode_source, settings_source, seconds=None, source=None):
        call = {"host": host, "session_id": sid, "tier": tier, "mode": mode, "mode_source": mode_source,
                "project_home": str(self.project_home),
                "settings_digest": readback_settings.load(str(self.project_home))["digest"],
                "settings_source": settings_source, "lineage": "CON-20261117-01"}  # D1, D9
        if seconds is not None:
            call.update(deadline_seconds=seconds, deadline_source=source,
                        round={"attempt": 1, "fails_before": 0, "authorization": None})  # D9
        return call

    def forget_rounds(self):
        """Start a new chain: a row that changes the settings of one lineage and tier begins without a ledger (D5)."""
        shutil.rmtree(self.home / "logbook" / ".readback" / "rounds", ignore_errors=True)

    def assert_no_records(self):
        self.assertFalse((self.common / "readback").exists())

    def assert_valid(self, contract, handoff, value):
        self.assertEqual(validate_handoff.validate(contract, handoff)["valid"], value)

    def bundle(self, name, omit=(), replace=None, oracle=False):
        folder = self.base / name
        if folder.exists():
            return folder / "outside_judge.py"
        folder.mkdir()
        if oracle:
            for target, path in (("outside_judge.py", "scripts/outside_judge.py"), ("judge.md", "agents/judge.md")):
                shown = subprocess.run(["git", "-C", str(ROOT), "show", BASE_SHA + ":" + path], capture_output=True,
                                       check=True, timeout=30)
                (folder / target).write_bytes(shown.stdout)
            return folder / "outside_judge.py"
        for item in BUNDLE_FILES:
            shutil.copy2(SCRIPTS / item, folder / item)
        shutil.copy2(ROOT / "agents" / "judge.md", folder / "judge.md")
        for item in omit:
            (folder / item).unlink()
        for item, text in (replace or {}).items():
            (folder / item).write_text(text)
        return folder / "outside_judge.py"


PD = {"kind": "project", "settings_source": "defaults"}
PF = {"kind": "project", "settings_source": "file"}
SESSION = {"kind": "session", "event_id": "ev-1", "at": AT}
TEXT_PD, TEXT_PF, TEXT_SESSION = "project defaults", "project settings", "session, judge line of " + AT


def off_note(source):
    return "judge mode off (" + source + "); no provider call"


def skipped_note(source):
    return "judge mode gate skips a step review (" + source + "); no provider call"


# ---------------------------------------------------------------------------------------------- ModeTests

class ModeTests(JudgeCase):
    def check_off_or_skipped(self, row, host, kind, sid, tier, mode, mode_source, settings_source, note):
        result = self.run_helper(host, tier, env=row)
        self.assertEqual(result.code, 0, result.out + result.err)
        self.assertEqual(result.word, kind)
        self.assertEqual(result.calls, [])
        self.assertEqual(result.note, note)
        records = self.records("no-review")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["result"], kind)
        self.assertEqual(records[0]["call"], self.expected_call(host, sid, tier, mode, mode_source, settings_source))
        self.assertFalse((self.common / "readback" / "reviews").exists())
        return result

    def check_call(self, row, host, sid, tier, mode, mode_source, settings_source, seconds):
        result = self.run_helper(host, tier, env=row)
        self.assertEqual(result.code, 0, result.out + result.err)
        self.assertEqual(result.word, "PASS")
        self.assertEqual(len(result.calls), 1)
        self.assertEqual(result.note, STARTED)
        expected = self.expected_call(host, sid, tier, mode, mode_source, settings_source, seconds,
                                      "settings")
        for kind in ("started", "completed"):
            records = self.records(kind)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["call"], expected)
        return result

    def check_none(self, row, host, tier, note):
        result = self.run_helper(host, tier, env=row)
        self.assertEqual(result.code, 3, result.out + result.err)
        self.assertEqual(result.word, "none")
        self.assertEqual(result.calls, [])
        self.assertEqual(result.note, note)
        self.assert_no_records()
        return result

    def test_outcome_table_gives_the_six_cells(self):
        cells = {("off", "step"): "off", ("off", "gate"): "off", ("gate", "step"): "skipped",
                 ("gate", "gate"): "call", ("each", "step"): "call", ("each", "gate"): "call"}
        for (mode, tier), expected in cells.items():
            with self.subTest(mode=mode, tier=tier):
                self.assertEqual(outside_judge.outcome(mode, tier), expected)

    def test_m1_no_file_no_id_gate_calls_with_project_defaults(self):
        self.check_call(None, "claude", None, "gate", "gate", PD, "defaults", 900)

    def test_m2_no_file_no_id_step_is_skipped(self):
        self.check_off_or_skipped(None, "claude", "skipped", None, "step", "gate", PD, "defaults",
                                  skipped_note(TEXT_PD))

    def test_m3_and_m4_mode_off_is_off_for_both_tiers(self):
        self.write_settings({"judge": {"mode": "off"}})
        for tier in ("gate", "step"):
            with self.subTest(tier=tier):
                shutil.rmtree(self.common / "readback", ignore_errors=True)
                self.check_off_or_skipped(None, "claude", "off", None, tier, "off", PF, "file", off_note(TEXT_PF))

    def test_m5_and_m6_mode_each_calls_on_the_route_of_the_tier(self):
        self.write_settings(ROUTES_FILE)
        for tier, model, effort, seconds in (("step", "gpt-6.2-sol", "medium", 300), ("gate", "gpt-6.2-astra", "xhigh", 900)):
            with self.subTest(tier=tier):
                shutil.rmtree(self.common / "readback", ignore_errors=True)
                result = self.check_call(None, "claude", None, tier, "each", PF, "file", seconds)
                self.assertEqual(model_args(result.calls[0]), (model, effort))

    def test_m7_session_off_beats_project_each(self):
        self.write_settings({"judge": {"mode": "each"}})
        self.write_session("claude", "sess-1", "off")
        self.check_off_or_skipped({"CLAUDE_CODE_SESSION_ID": "sess-1"}, "claude", "off", "sess-1", "gate", "off",
                                  SESSION, "file", off_note(TEXT_SESSION))

    def test_m8_session_each_beats_project_off_on_a_step(self):
        self.write_settings({"judge": {"mode": "off"}})
        self.write_session("claude", "sess-1", "each")
        self.check_call({"CLAUDE_CODE_SESSION_ID": "sess-1"}, "claude", "sess-1", "step", "each", SESSION, "file",
                        300)

    def test_m9_and_m10_session_gate_skips_a_step_and_calls_a_gate(self):
        self.write_settings({"judge": {"mode": "off"}})
        self.write_session("claude", "sess-1", "gate")
        row = {"CLAUDE_CODE_SESSION_ID": "sess-1"}
        self.check_off_or_skipped(row, "claude", "skipped", "sess-1", "step", "gate", SESSION, "file",
                                  skipped_note(TEXT_SESSION))
        shutil.rmtree(self.common / "readback")
        self.check_call(row, "claude", "sess-1", "gate", "gate", SESSION, "file", 900)

    def test_m11_a_session_mode_set_to_null_falls_back_to_the_project(self):
        self.write_settings({"judge": {"mode": "each"}})
        self.write_session("claude", "sess-1", "off", None)
        state = readback_session.read("claude", "sess-1", base=str(self.sessions))
        self.assertEqual((state[0], state[1]["mode"]), ("valid", None))
        self.check_call({"CLAUDE_CODE_SESSION_ID": "sess-1"}, "claude", "sess-1", "step", "each", PF, "file", 300)

    def test_m12_an_id_without_a_state_file_uses_the_project_mode(self):
        self.write_settings({"judge": {"mode": "off"}})
        self.check_off_or_skipped({"CLAUDE_CODE_SESSION_ID": "no-file"}, "claude", "off", "no-file", "gate", "off",
                                  PF, "file", off_note(TEXT_PF))

    def test_m13_a_state_file_that_is_not_json_gives_none(self):
        self.write_settings({"judge": {"mode": "off"}})
        self.write_bad_session("claude", "sess-1")
        self.check_none({"CLAUDE_CODE_SESSION_ID": "sess-1"}, "claude", "gate",
                        "no verdict: the session state is invalid")

    def test_m14_an_unsafe_session_variable_is_no_id(self):
        self.write_settings({"judge": {"mode": "off"}})
        self.check_off_or_skipped({"CLAUDE_CODE_SESSION_ID": "../x"}, "claude", "off", None, "gate", "off", PF,
                                  "file", off_note(TEXT_PF))

    def test_m19_a_state_with_a_mode_and_a_null_mode_event_gives_none(self):
        self.write_settings({"judge": {"mode": "off"}})
        self.sessions.mkdir(parents=True)
        (self.sessions / "claude-sess-1.json").write_text(json.dumps({
            "schema_version": 1, "host": "claude", "session_id": "sess-1", "mode": "off", "mode_event": None,
            "authorizations": [], "processed_events": []}))
        self.assertEqual(readback_session.read("claude", "sess-1", base=str(self.sessions))[0], "valid")
        self.check_none({"CLAUDE_CODE_SESSION_ID": "sess-1"}, "claude", "gate",
                        "no verdict: the session state is invalid")

    def test_m15_a_settings_file_with_an_unknown_key_gives_none(self):
        self.write_settings({"judge": {"mode": "off"}, "unknown": 1})
        self.check_none(None, "claude", "gate", "no verdict: settings: INVALID_SETTINGS")

    def test_m16_a_registry_that_is_not_json_gives_the_resolve_code(self):
        registry = self.home / "logbook" / ".readback" / "projects.json"
        registry.parent.mkdir(parents=True)
        registry.write_text("not json")
        with self.assertRaises(readback_settings.SettingsError) as raised:
            with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
                readback_settings.resolve(self.repo, "claude")
        self.assertEqual(raised.exception.code, "INVALID")
        self.check_none(None, "claude", "gate", "no verdict: settings: INVALID")

    def test_m17_the_session_variable_of_the_host_selects_the_state(self):
        self.write_settings({"judge": {"mode": "each"}})
        self.write_session("codex", "codex-id", "off")
        self.write_session("claude", "claude-id", "each")
        row = {"CODEX_SESSION_ID": "codex-id", "CLAUDE_CODE_SESSION_ID": "claude-id"}
        self.check_off_or_skipped(row, "codex", "off", "codex-id", "gate", "off", SESSION, "file",
                                  off_note(TEXT_SESSION))

    def test_m18_another_tier_gives_none_and_the_default_tier_is_gate(self):
        result = self.run_helper("claude", "other")
        self.assertEqual((result.code, result.word, result.calls), (3, "none", []))
        self.assertTrue(result.note.startswith("no verdict: invalid arguments"), result.note)
        self.assert_no_records()
        self.write_settings({"judge": {"mode": "gate"}})
        result = self.run_helper("claude")  # no --tier: gate, so the provider is called
        self.assertEqual((result.code, result.word, len(result.calls)), (0, "PASS", 1))
        self.assertEqual(self.records("started")[0]["call"]["tier"], "gate")


# ------------------------------------------------------------------------------------ ValidationGateTests

class ValidationGateTests(JudgeCase):
    def set_fixture(self, name):
        if name == "true":
            self.put(CONTRACT_TRUE, HANDOFF_TRUE)
            self.assert_valid(CONTRACT_TRUE, HANDOFF_TRUE, True)
        elif name == "false":
            self.put(CONTRACT_TRUE, HANDOFF_FALSE)
            self.assert_valid(CONTRACT_TRUE, HANDOFF_FALSE, False)
        elif name == "unparsed":
            self.put(CONTRACT_TRUE, HANDOFF_UNPARSED)
            self.assert_valid(CONTRACT_TRUE, HANDOFF_UNPARSED, "unparsed")
        else:
            self.put(CONTRACT_TRUE, HANDOFF_TRUE)
        return validate_handoff.validate(self.contract.read_text(), self.handoff.read_text())

    def helper_for(self, name):
        if name != "unavailable":
            return HELPER
        return self.bundle("raising", replace={"validate_handoff.py": "def validate(c, h):\n    raise RuntimeError('x')\n"})

    def cell(self, outcome, name):
        """Run one cell. Return (result, validation object of the fixture)."""
        validation = self.set_fixture(name)
        if name == "unavailable":
            validation = {"valid": "unavailable", "reason": "validator raised"}
        settings = {"judge": {"mode": "off"}} if outcome == "off" else {"judge": {"mode": "gate"}}
        self.write_settings(settings)
        tier = "step" if outcome == "skipped" else "gate"
        return self.run_helper("claude", tier, helper=self.helper_for(name)), validation

    def phrase(self, name, validation):
        if name == "false":
            return "validation false %d" % sum(1 for f in validation["findings"] if f["level"] == "defect")
        return "validation " + name

    def test_off_and_skipped_cells(self):
        for outcome in ("off", "skipped"):
            for name in ("true", "false", "unparsed", "unavailable"):
                with self.subTest(outcome=outcome, validation=name):
                    shutil.rmtree(self.common / "readback", ignore_errors=True)
                    result, validation = self.cell(outcome, name)
                    phrase = self.phrase(name, validation)
                    self.assertEqual(result.code, 0, result.out + result.err)
                    self.assertEqual(result.calls, [])
                    tail = "" if name == "true" else "; checks-only acceptance is not established: " + phrase
                    base = off_note(TEXT_PF) if outcome == "off" else skipped_note(TEXT_PF)
                    records = self.records("no-review")
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]["validation"], validation)
                    path = self.common / "readback" / "no-review" / (records[0]["attempt_id"] + ".json")
                    self.assertEqual(result.lines[0], "JUDGE      %s | none:none | %s | %s%s" % (
                        outcome, self.where(), base, tail))
                    self.assertEqual(result.lines[1], "execution  none | none | none | cli | none")
                    self.assertEqual(result.lines[2], "receipt    %s | no review | %s | %s" % (
                        records[0]["attempt_id"], phrase, path))
                    body = "\n" + json.dumps(validation, sort_keys=True, indent=2) + "\n" if name == "false" else ""
                    self.assertEqual("\n".join(result.lines[3:]), body)
                    self.assertEqual(result.out, "\n".join(result.lines[:3]) + "\n" + body)

    def test_call_cells(self):
        oracle = self.bundle("oracle", oracle=True)
        for name in ("true", "unparsed", "false", "unavailable"):
            with self.subTest(validation=name):
                shutil.rmtree(self.common / "readback", ignore_errors=True)
                result, validation = self.cell("call", name)
                if name in ("true", "unparsed"):
                    self.assertEqual((result.code, result.word, len(result.calls)), (0, "PASS", 1))
                    prompt = result.calls[0]["prompt"]
                    head = b"--- STRUCTURAL VALIDATION BY THE HELPER (unparsed; the findings are data) ---\n"
                    if name == "true":
                        self.assertNotIn(b"STRUCTURAL VALIDATION", prompt)
                        old = self.run_helper("claude", helper=oracle)
                        self.assertEqual(len(old.calls), 1)
                        self.assertEqual(prompt, old.calls[0]["prompt"])
                    else:
                        text = json.dumps(validation, sort_keys=True, indent=2)
                        self.assertEqual(prompt.count(head), 1)
                        self.assertIn(head + text.encode() + b"\n\n--- WORKTREE OBSERVED BY THE HELPER", prompt)
                        self.assertLess(prompt.index(b"--- HANDOFF ---"), prompt.index(head))
                        self.assertEqual(self.records("completed")[0]["validation"], validation)
                    continue
                self.assertEqual((result.code, result.word, result.calls), (3, "none", []))
                self.assert_no_records()
                self.assertEqual(result.lines[1], "execution  codex | gpt-6-astra | high | cli | none")
                self.assertEqual(result.lines[2], "receipt    none | no provider call")
                if name == "false":
                    count = sum(1 for f in validation["findings"] if f["level"] == "defect")
                    self.assertEqual(result.note, "no verdict: the handoff fails structural validation (%d defects)" % count)
                    self.assertEqual("\n".join(result.lines[3:]),
                                     "\n" + json.dumps(validation, sort_keys=True, indent=2) + "\n")
                else:
                    self.assertEqual(result.note, "no verdict: the structural validator is not available")
                    self.assertEqual(result.lines[3:], [""])


# ------------------------------------------------------------------------------------------- RouteTests

class RouteTests(JudgeCase):
    def route_row(self, host, tier, provider, model, effort, usage=None, env=None, source="settings",
                  match="UNVERIFIED", word="PASS", code=0):
        extra = dict(env or {})
        if usage is not None:
            extra["FAKE_USAGE"] = json.dumps({name: {} for name in usage})
        result = self.run_helper(host, tier, env=extra)
        self.assertEqual((result.code, result.word), (code, word), result.out + result.err)
        self.assertEqual(len(result.calls), 1)
        self.assertEqual(result.calls[0]["provider"], provider)
        self.assertEqual(model_args(result.calls[0]), (model, effort))
        for kind in ("started", "completed"):
            records = self.records(kind)
            self.assertEqual(len(records), 1)
            route = records[0]["route"]
            self.assertEqual((route["provider"], route["model"], route["effort"], route["source"]),
                             (provider, model, effort, source))
        done = self.records("completed")[0]
        self.assertEqual(done["judge"]["model_match"], match)
        return result, done

    def none_before_provider(self, host, tier, env=None):
        result = self.run_helper(host, tier, env=env)
        self.assertEqual((result.code, result.word, result.calls), (3, "none", []))
        self.assert_no_records()
        return result

    def test_r1_to_r4_the_route_of_the_host_and_tier(self):
        self.write_settings(ROUTES_FILE)
        rows = (("R1", "claude", "step", "codex", "gpt-6.2-sol", "medium", None),
                ("R2", "claude", "gate", "codex", "gpt-6.2-astra", "xhigh", None),
                ("R3", "codex", "step", "claude", "claude-sonnet-5-5", "low", ["claude-sonnet-5-5"]),
                ("R4", "codex", "gate", "claude", "claude-opus-5-5", "medium", ["claude-opus-5-5"]))
        for name, host, tier, provider, model, effort, usage in rows:
            with self.subTest(row=name):
                shutil.rmtree(self.common / "readback", ignore_errors=True)
                self.route_row(host, tier, provider, model, effort, usage, match="UNVERIFIED" if usage is None else "VERIFIED")

    def test_r5_and_r6_a_file_hides_the_table_also_when_it_is_invalid(self):
        self.write_settings({"judge": {"mode": "each"}})
        table = self.base / "TEAMMATES.md"
        for name, rows in (("R5", "judge | claude | codex | gpt-9-table | low\n"),
                           ("R6", "judge | claude | other | model | high\n")):
            with self.subTest(row=name):
                shutil.rmtree(self.common / "readback", ignore_errors=True)
                table.write_text(rows)
                self.route_row("claude", "gate", "codex", "gpt-6-astra", "high", env={"READBACK_TEAMMATES": str(table)})

    def test_r7_to_r9_without_a_file_the_table_or_the_default_decides(self):
        table = self.base / "TEAMMATES.md"
        table.write_text("judge | claude | codex | gpt-6.2-sol | xhigh\n")
        env = {"READBACK_TEAMMATES": str(table)}
        self.route_row("claude", "gate", "codex", "gpt-6.2-sol", "xhigh", env=env, source="table")
        shutil.rmtree(self.common / "readback")
        self.write_session("claude", "sess-1", "each")
        self.route_row("claude", "step", "codex", "gpt-6.2-sol", "xhigh",
                       env=dict(env, CLAUDE_CODE_SESSION_ID="sess-1"), source="table")
        shutil.rmtree(self.common / "readback")
        self.route_row("claude", "gate", "codex", "gpt-6-astra", "high", source="defaults")

    def test_r10_to_r14_the_reported_models_of_a_claude_judge(self):
        self.write_settings({"judge": {"mode": "each", "routes": {"claude": {"gate": {"model": "claude-opus-5-5", "effort": "medium"}}}}})
        result, done = self.route_row("codex", "gate", "claude", "claude-opus-5-5", "medium", ["claude-opus-5-5"], match="VERIFIED")
        self.assertEqual(done["judge"]["reported_models"], ["claude-opus-5-5"])
        self.assertEqual(result.note, STARTED)
        shutil.rmtree(self.common / "readback")
        result, done = self.route_row("codex", "gate", "claude", "claude-opus-5-5", "medium", ["claude-sonnet-5-5"],
                                      word="none", code=3)  # R11
        self.assertEqual(done["judge"]["verdict"], "none")
        for name, model, usage, word, code, match in (
                ("R12", "opus", ["claude-opus-5-5"], "PASS", 0, "UNVERIFIED"),
                ("R13", "fable", ["claude-fable-5-2"], "PASS", 0, "UNVERIFIED"),
                ("R14", "fable", ["claude-opus-5-5"], "none", 3, "UNVERIFIED")):
            with self.subTest(row=name):
                shutil.rmtree(self.common / "readback")
                self.forget_rounds()
                self.write_settings({"judge": {"mode": "each", "routes": {"claude": {"gate": {"model": model, "effort": "medium"}}}}})
                result, _done = self.route_row("codex", "gate", "claude", model, "medium", usage, word=word, code=code, match=match)
                if word == "PASS":
                    self.assertTrue(result.note.endswith("; reported model: " + usage[0]), result.note)

    def test_r15_the_fable_variable_matters_only_to_a_fable_request(self):
        env = {"ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-opus-5-5"}
        self.write_settings({"judge": {"mode": "each", "routes": {"claude": {"gate": {"model": "claude-opus-5-5", "effort": "medium"}}}}})
        self.route_row("codex", "gate", "claude", "claude-opus-5-5", "medium", ["claude-opus-5-5"], env=env, match="VERIFIED")
        shutil.rmtree(self.common / "readback")
        self.write_settings({"judge": {"mode": "each", "routes": {"claude": {"gate": {"model": "fable", "effort": "medium"}}}}})
        result = self.none_before_provider("codex", "gate", env)
        self.assertEqual(result.note, "no verdict: ANTHROPIC_DEFAULT_FABLE_MODEL points outside the Fable family")

    def test_r16_a_table_row_for_a_claude_judge_outside_the_fable_family_gives_none(self):
        table = self.base / "TEAMMATES.md"
        table.write_text("judge | claude | opus | high\n")
        result = self.none_before_provider("codex", "gate", {"READBACK_TEAMMATES": str(table)})
        self.assertEqual(result.note, "no verdict: the Claude judge must use the Fable family")

    def test_r17_a_codex_route_has_unattested_models(self):
        self.write_settings(ROUTES_FILE)
        _result, done = self.route_row("claude", "gate", "codex", "gpt-6.2-astra", "xhigh")
        self.assertEqual(done["judge"]["reported_models"], "unattested")
        self.assertEqual(done["judge"]["model_match"], "UNVERIFIED")

    def deadline_row(self, tier, expected, source, env=None, host="claude"):
        result = self.run_helper(host, tier, env=env)
        self.assertEqual((result.code, result.word), (0, "PASS"), result.out + result.err)
        for kind in ("started", "completed"):
            call = self.records(kind)[0]["call"]
            self.assertEqual((call["deadline_seconds"], call["deadline_source"]), (expected, source))
            self.assertIs(type(call["deadline_seconds"]), type(expected))  # the parsed JSON value: int, never 45.0
        shutil.rmtree(self.common / "readback")
        self.forget_rounds()

    def test_t1_t2_t3_t5_the_deadline_and_its_source(self):
        self.deadline_row("gate", 900, "settings")  # T1
        self.write_session("claude", "sess-1", "each")
        self.deadline_row("step", 300, "settings", {"CLAUDE_CODE_SESSION_ID": "sess-1"})  # T5
        self.write_settings({"judge": {"mode": "each", "deadline_seconds": {"step": 45, "gate": 60}}})
        self.deadline_row("step", 45, "settings")  # T2
        self.deadline_row("gate", 60, "settings")
        for tier in ("step", "gate"):  # T3
            self.deadline_row(tier, 7, "env", {"READBACK_JUDGE_SECONDS": "7"})

    def test_deadline_values_that_are_not_positive_numbers_give_none_and_7_5_stays_a_float(self):
        for value in ("0", "-1", "inf", "nan", "abc"):
            with self.subTest(value=value):
                result = self.none_before_provider("claude", "gate", {"READBACK_JUDGE_SECONDS": value})
                self.assertEqual(result.note, "no verdict: the deadline must be a positive number")
        self.deadline_row("gate", 7.5, "env", {"READBACK_JUDGE_SECONDS": "7.5"})
        self.deadline_row("gate", 1, "env", {"READBACK_JUDGE_SECONDS": "1"})  # the smallest whole second is accepted
        self.deadline_row("gate", 0.5, "env", {"READBACK_JUDGE_SECONDS": "0.5"})

    def test_a_fable_route_with_another_reported_model_gives_none(self):
        self.write_settings({"judge": {"mode": "each", "routes": {"claude": {"gate": {"model": "claude-fable-5-2", "effort": "medium"}}}}})
        for usage, note in (("claude-fable-5-1", "Claude reported a model different from the requested model"),
                            ("claude-opus-5-5", "Claude reported a model outside the requested Fable family")):
            with self.subTest(reported=usage):
                shutil.rmtree(self.common / "readback", ignore_errors=True)
                result, _done = self.route_row("codex", "gate", "claude", "claude-fable-5-2", "medium", [usage],
                                               word="none", code=3)
                self.assertEqual(result.note, "no verdict: " + note)

    def test_t4_a_deadline_that_is_not_a_number_gives_none(self):
        result = self.none_before_provider("claude", "gate", {"READBACK_JUDGE_SECONDS": "abc"})
        self.assertEqual(result.note, "no verdict: the deadline must be a positive number")

    def test_t2_and_t3_the_seconds_that_run_process_receives(self):
        self.write_settings({"judge": {"mode": "each", "deadline_seconds": {"step": 45, "gate": 60}}})
        seen = []

        def fake(command, prompt, repo, env, seconds):
            seen.append(seconds)
            Path(command[command.index("-o") + 1]).write_text("PASS ok\n")
            return ""
        for extra, tier, expected in (({}, "step", 45.0), ({}, "gate", 60.0),
                                      ({"READBACK_JUDGE_SECONDS": "7"}, "step", 7.0),
                                      ({"READBACK_JUDGE_SECONDS": "7"}, "gate", 7.0)):
            with self.subTest(extra=extra, tier=tier):
                del seen[:]
                shutil.rmtree(self.common / "readback", ignore_errors=True)
                with mock.patch.dict(os.environ, self.base_env(extra), clear=True), \
                        mock.patch.object(outside_judge, "run_process", fake), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    code = outside_judge.main(["--host", "claude", "--tier", tier, str(self.contract),
                                               str(self.handoff), str(self.repo)])
                self.assertEqual(code, 0, out.getvalue())
                self.assertEqual(seen, [expected])


# ----------------------------------------------------------------------------------------- RecordTests

class RecordTests(JudgeCase):
    def test_expiry_on_next_judge_use_preserves_the_fail_limit(self):
        self.write_settings({"judge": {"max_fail_rounds": 1}})
        original = self.run_helper(env={"FAKE_ANSWER": "FAIL Missing proof. password=fixture_value"})
        self.assertEqual(original.word, "FAIL", original.out + original.err)
        store = self.common / "readback"
        rounds = self.home / "logbook" / ".readback" / "rounds"
        receipts = list((store / "reviews").rglob("*.json"))
        marker = next(p for p in (store / "by-manifest").rglob("*") if p.is_file())
        payloads = list(rounds.rglob("*.txt"))
        self.assertEqual((len(receipts), len(payloads)), (2, 3))
        for path in receipts + payloads:
            self.assertNotIn(b"fixture_value", path.read_bytes())
        old = "2000-01-01T00:00:00Z"
        for path in receipts:
            record = json.loads(path.read_bytes())
            record["created"] = old
            path.write_text(json.dumps(record))
        for path in receipts + payloads + [marker]:
            os.utime(path, (946684800, 946684800))
        ledger = next(rounds.glob("*.json"))
        state = json.loads(ledger.read_bytes())
        state["attempts"][0].update(reserved_at=old, finished_at=old)
        ledger.write_text(json.dumps(state))
        retry = self.run_helper()
        self.assertIn("round limit (1 FAIL", retry.note)
        self.assertEqual(retry.calls, [])
        self.assertEqual(json.loads(ledger.read_bytes()), state)
        self.assertFalse(any(p.exists() for p in receipts + payloads + [marker]))

    def test_the_call_object_is_in_the_started_and_the_completed_record(self):
        self.write_settings({"judge": {"mode": "each"}})
        self.write_session("claude", "sess-1", "each")
        result = self.run_helper("claude", "step", env={"CLAUDE_CODE_SESSION_ID": "sess-1"})
        self.assertEqual((result.code, result.word), (0, "PASS"))
        expected = self.expected_call("claude", "sess-1", "step", "each", SESSION, "file", 300, "settings")
        self.assertEqual(sorted(expected), sorted(["host", "session_id", "tier", "mode", "mode_source", "project_home",
                                                    "settings_digest", "settings_source", "deadline_seconds",
                                                    "deadline_source", "lineage", "round"]))
        started, completed = self.records("started")[0], self.records("completed")[0]
        self.assertEqual(started["call"], expected)
        self.assertEqual(completed["call"], expected)
        self.assertEqual(started["schema_version"], 1)
        self.assertEqual(completed["schema_version"], 1)
        self.assertEqual(completed["judge"]["model_match"], "UNVERIFIED")

    def test_exit_status_of_the_completed_record(self):
        self.write_settings({"judge": {"mode": "each", "routes": {"claude": {"gate": {"model": "claude-opus-5-5", "effort": "medium"}}}}})
        self.run_helper("codex", "gate", env={"FAKE_USAGE": json.dumps({"claude-opus-5-5": {}})})
        self.assertEqual(self.records("completed")[0]["exit_status"], 0)
        shutil.rmtree(self.common / "readback")
        result = self.run_helper("codex", "gate", env={"FAKE_USAGE": json.dumps({"claude-sonnet-5-5": {}})})
        self.assertEqual(result.code, 3)
        self.assertEqual(self.records("completed")[0]["exit_status"], 3)

    def test_the_helper_without_review_receipt_or_validate_handoff(self):
        for omitted in ("review_receipt.py", "validate_handoff.py"):
            helper = self.bundle("without-" + omitted, omit=(omitted,))
            with self.subTest(omitted=omitted, cell="call"):
                self.write_settings({"judge": {"mode": "gate"}})
                result = self.run_helper("claude", "gate", helper=helper)
                self.assertEqual((result.code, result.word, result.calls), (3, "none", []))
                self.assertEqual(result.note, "no verdict: the structural validator is not available")
                self.assert_no_records()
            with self.subTest(omitted=omitted, cell="off"):
                self.write_settings({"judge": {"mode": "off"}})
                result = self.run_helper("claude", "gate", helper=helper)
                self.assertEqual((result.code, result.word, result.calls), (0, "off", []))
                self.assertEqual(result.note, off_note(TEXT_PF)
                                 + "; checks-only acceptance is not established: validation unavailable")
                if omitted == "review_receipt.py":
                    self.assertEqual(result.lines[2], "receipt    none | modules not available")
                    self.assert_no_records()
                else:
                    record = self.records("no-review")[0]
                    self.assertEqual(record["validation"], {"valid": "unavailable", "reason": "validator not importable"})
                    self.assertEqual(result.lines[2], "receipt    %s | no review | validation unavailable | %s" % (
                        record["attempt_id"], self.common / "readback" / "no-review" / (record["attempt_id"] + ".json")))

    def test_the_repository_lookups_of_an_off_record(self):
        inputs = (self.contract, self.handoff, self.contract.read_bytes(), self.handoff.read_bytes())
        validation = {"valid": True, "findings": []}
        call = {"host": "claude"}
        real_info, real_common = outside_judge.review_receipt.repository_info, outside_judge.review_capture.git_common_dir

        def boom(_repo):
            raise OSError("x")
        cases = (("info None", {"repository_info": lambda r: None}, "receipt    none | not a Git worktree"),
                 ("common None", {"git_common_dir": lambda r: None}, "receipt    none | not a Git worktree"),
                 ("info raises", {"repository_info": boom}, "receipt    none | cannot read the repository"),
                 ("common raises", {"git_common_dir": boom}, "receipt    none | cannot read the repository"))
        for name, patch, expected in cases:
            with self.subTest(case=name):
                with contextlib.ExitStack() as stack:
                    for attr, fake in patch.items():
                        target = outside_judge.review_receipt if attr == "repository_info" else outside_judge.review_capture
                        stack.enter_context(mock.patch.object(target, attr, fake))
                    self.assertEqual(outside_judge.no_review_receipt(self.repo, "off", inputs, validation, call), expected)
        self.assertIs(outside_judge.review_receipt.repository_info, real_info)
        self.assertIs(outside_judge.review_capture.git_common_dir, real_common)
        self.assert_no_records()

    def test_the_record_of_off_and_skipped(self):
        self.write_settings({"judge": {"mode": "off"}})
        result = self.run_helper("claude", "gate")
        self.assertEqual(result.word, "off")
        folder = self.common / "readback" / "no-review"
        files = sorted(folder.iterdir())
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text())
        self.assertEqual(sorted(record), sorted(["schema_version", "attempt_id", "created", "result", "repository",
                                                  "contract", "handoff", "validation", "call", "helper_version"]))
        self.assertEqual(files[0].name, record["attempt_id"] + ".json")
        self.assertEqual((record["schema_version"], record["result"]), (1, "off"))
        self.assertEqual(record["contract"], {"path": str(self.contract),
                                              "sha256": review_receipt.sha256_hex(self.contract.read_bytes())})
        self.assertEqual(record["handoff"], {"path": str(self.handoff),
                                             "sha256": review_receipt.sha256_hex(self.handoff.read_bytes())})
        self.assertEqual(record["helper_version"], {"file_sha256": review_receipt.sha256_hex(HELPER.read_bytes())})
        self.assertEqual(record["validation"], validate_handoff.validate(CONTRACT_TRUE, HANDOFF_TRUE))
        self.assertEqual(sorted(record["call"]), sorted(["host", "session_id", "tier", "mode", "mode_source",
                                                         "project_home", "settings_digest", "settings_source",
                                                         "lineage"]))
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.common / "readback").stat().st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in (self.common / "readback").iterdir()), ["no-review"])
        self.assertEqual(record["repository"]["root_name"], "repo")
        # no reader of the review records sees it
        self.assertEqual(review_receipt.list_unfinished(str(self.common)), {"indeterminate": [], "partial": []})
        self.assertEqual(review_receipt.duplicates_of(str(self.common), "k" * 64, "x"), [])
        self.write_settings({"judge": {"mode": "gate"}})
        self.run_helper("claude", "gate")  # a call: one review under one key
        key = self.records("completed")[0]["review_key"]
        self.assertEqual(review_receipt.duplicates_of(str(self.common), key, "x"), [self.records("completed")[0]["attempt_id"]])
        self.assertEqual(review_receipt.list_unfinished(str(self.common)), {"indeterminate": [], "partial": []})
        marked = [p.name for p in (self.common / "readback" / "by-manifest").rglob("*") if p.is_file()]
        self.assertEqual(marked, [self.records("completed")[0]["attempt_id"]])
        self.assertNotIn(record["attempt_id"], marked)

    def test_a_record_that_cannot_be_stored_gives_one_of_the_none_lines(self):
        self.write_settings({"judge": {"mode": "off"}})
        (self.common / "readback").write_text("a file where the folder belongs")
        result = self.run_helper("claude", "gate")
        self.assertEqual((result.code, result.word), (0, "off"))
        self.assertEqual(result.lines[2], "receipt    none | cannot write the record")
        (self.common / "readback").unlink()
        plain = self.base / "plain"
        plain.mkdir()
        (self.home / "logbook" / "plain").mkdir()
        (self.home / "logbook" / "plain" / "settings.json").write_text('{"schema_version": 1, "judge": {"mode": "off"}}')
        result = self.run_helper("claude", "gate", repo=plain)
        self.assertEqual((result.code, result.word, result.lines[2]), (0, "off", "receipt    none | not a Git worktree"))
        bundle = self.bundle("nocapture", omit=("review_capture.py",))
        result = self.run_helper("claude", "gate", helper=bundle)
        self.assertEqual((result.code, result.word, result.lines[2]), (0, "off", "receipt    none | modules not available"))
        self.assert_no_records()

    def test_a_record_of_part_1_without_call_stays_valid(self):
        capture = {"state": "ok", "reason": None, "manifest_sha256": "a" * 64}
        route = {"provider": "codex", "model": "gpt-6-astra", "effort": "high", "source": "defaults"}
        started = review_receipt.started_record("b" * 32, "k" * 64, AT, {"root_name": "r"}, capture, "/home/user/c", "1" * 64,
                                                "/home/user/h", "2" * 64, "3" * 64, route, {"file_sha256": "4" * 64})
        self.assertNotIn("call", started)
        completed = review_receipt.completed_record(started, capture, "ends-equal", {"valid": True, "findings": []},
                                                    review_receipt.judge_object("PASS", "codex", "gpt-6-astra", "high", None),
                                                    "5" * 64, 0, [])
        self.assertNotIn("call", completed)
        self.assertNotIn("model_match", completed["judge"])
        folder = self.common / "readback" / "reviews" / ("k" * 64)
        folder.mkdir(parents=True)
        review_receipt.store_started(str(self.common), started)
        found = review_receipt._scan(str(folder))
        self.assertEqual(found, (["b" * 32], [], []))
        review_receipt.store_completed(str(self.common), completed)
        self.assertEqual(review_receipt._scan(str(folder)), (["b" * 32], ["b" * 32], []))
        self.assertEqual(review_receipt.list_unfinished(str(self.common)), {"indeterminate": [], "partial": []})

    def test_the_helper_gives_a_verdict_from_scripts_and_from_the_portable_copy(self):
        for helper in (HELPER, PORTABLE):
            with self.subTest(helper=str(helper.parent.parent.name)):
                self.assertTrue(helper.is_file())
                shutil.rmtree(self.common / "readback", ignore_errors=True)
                result = self.run_helper("codex", "gate", helper=helper)
                self.assertEqual((result.code, result.word, len(result.calls)), (0, "PASS", 1), result.out + result.err)
                self.assertEqual(len(self.records("completed")), 1)

    def test_a_settings_or_session_module_that_cannot_be_imported_gives_none(self):
        cases = {"missing settings": dict(omit=("readback_settings.py",)),
                 "missing session": dict(omit=("readback_session.py",)),
                 "settings raises": dict(replace={"readback_settings.py": "raise ImportError('x')\n"}),
                 "session raises": dict(replace={"readback_session.py": "raise RuntimeError('x')\n"})}
        for name, spec in cases.items():
            with self.subTest(case=name):
                helper = self.bundle("b-" + name.replace(" ", "-"), **spec)
                result = self.run_helper("claude", "gate", helper=helper)
                self.assertEqual((result.code, result.word, result.calls), (3, "none", []))
                self.assertEqual(result.note, "no verdict: the settings modules are not available")
                self.assertEqual(result.lines[2], "receipt    none | no provider call")
                self.assert_no_records()


if __name__ == "__main__":
    unittest.main()
