#!/usr/bin/env python3
"""Offline checks for hooks/guard_dispatch.py.

Run: python3 -m unittest discover -s checks -p 'test_guard_dispatch.py' -v
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "guard_dispatch.py"
CONFIG = ROOT / "hooks" / "hooks.json"

INLINE = (
    "Do the work of contract CON-20261119-08.\n"
    "ACC-1  The hook shall print nothing for a plain command.   Check: python3 -m unittest\n"
    "Out of scope: app/\n"
)


def event(prompt, tool="Agent", agent="readback:worker", **extra):
    data = {"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": ".",
            "tool_input": {"prompt": prompt, "subagent_type": agent}}
    data.update(extra)
    return data


class DispatchTests(unittest.TestCase):
    def run_hook(self, payload, argv=("claude",)):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, str(HOOK), *argv], input=text,
                              capture_output=True, text=True, timeout=10)

    def assert_silent(self, payload, **kw):
        done = self.run_hook(payload, **kw)
        self.assertEqual((done.returncode, done.stdout), (0, ""), done.stderr)

    def assert_denied(self, payload, ids):
        done = self.run_hook(payload)
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)
        self.assertEqual(list(out), ["hookSpecificOutput"])
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "PreToolUse")
        self.assertEqual(spec["permissionDecision"], "deny")
        for one in ids:
            self.assertIn(one, spec["permissionDecisionReason"])
        self.assertIn("statement", spec["permissionDecisionReason"])
        self.assertIn(".md", spec["permissionDecisionReason"])
        return spec["permissionDecisionReason"]

    # denied shapes
    def test_id_only(self):
        self.assert_denied(event("Package WA of contract CON-20261119-08"), ["CON-20261119-08"])

    def test_task_tool_name(self):
        self.assert_denied(event("Package WA of contract CON-20261119-08", tool="Task"), ["CON-20261119-08"])

    def test_bare_acc_and_sentence_acc(self):
        self.assert_denied(event("CON-20261119-08\nACC-1\n"), ["CON-20261119-08"])
        self.assert_denied(event("CON-20261119-08 Please satisfy ACC-1 and ACC-2 in the work"), ["CON-20261119-08"])
        self.assert_denied(event("CON-20261119-08\nACC-1 shall hold"), ["CON-20261119-08"])

    def test_missing_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "records" / "CON-20261119-08.md"
            self.assert_denied(event(f"Read `{missing}` for CON-20261119-08"), ["CON-20261119-08"])

    def test_missing_tilde_path(self):
        self.assert_denied(event("Read ~/no-such-dir-xyz/CON-20261119-08.md"), ["CON-20261119-08"])

    def test_two_ids_one_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "CON-20261119-01.md"
            good.write_text("x", encoding="utf-8")
            reason = self.assert_denied(event(f"CON-20261119-01 at {good} and CON-20261119-02"), ["CON-20261119-02"])
            self.assertNotIn("CON-20261119-01", reason)

    def test_path_of_other_id_does_not_deliver(self):
        reason = self.assert_denied(event("CON-20261119-08 CON-20261119-01 see ROOT/CON-20261119-01.md"),
                                    ["CON-20261119-08"])
        self.assertNotIn("CON-20261119-01", reason)

    # silent shapes
    def test_backticked_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "CON-20261119-08.md"
            good.write_text("x", encoding="utf-8")
            self.assert_silent(event(f"Contract CON-20261119-08 is in `{good}`."))
            self.assert_silent(event(f'Contract CON-20261119-08: ("{good}").'))

    def test_relative_path_under_root(self):
        self.assert_silent(event("Use ROOT/evidence/CON-20261119-01.md for CON-20261119-01"))
        self.assert_silent(event("Read [CON-20261119-01.md], then work on CON-20261119-01."))

    def test_two_ids_two_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            one = Path(tmp) / "CON-20261119-01.md"
            one.write_text("x", encoding="utf-8")
            self.assert_silent(event(f"CON-20261119-01 {one}\nCON-20261119-02 records/CON-20261119-02.md"))

    def test_inline_with_check(self):
        self.assert_silent(event(INLINE))

    def test_inline_in_table_and_quote(self):
        self.assert_silent(event("CON-20261119-08\n| ACC-1 | The hook shall deny | Check: run tests |"))
        self.assert_silent(event("CON-20261119-08\n> ACC-1  The hook shall deny it.  Check: run"))
        self.assert_silent(event("CON-20261119-08\n- ACC-1 The hook shall deny it"))
        self.assert_silent(event("CON-20261119-08\n  * ACC-1: The hook shall deny it"))

    def test_inline_delivers_all_ids(self):
        self.assert_silent(event("CON-20261119-08 CON-20261119-09\nACC-1 The hook shall deny it"))

    def test_marker_boundary_is_not_a_statement(self):
        for line in ("ACC-1suffix two words", "ACC-1a The hook shall deny", "ACC-1-2 The hook shall deny",
                     "ACC-1_x The hook shall deny", "ACC- The hook shall deny", "ACC-x The hook shall deny",
                     "acc-1 The hook shall deny", "Acc-1 The hook shall deny", "xACC-1 The hook shall deny"):
            with self.subTest(line=line):
                self.assert_denied(event("CON-20261119-08\n" + line), ["CON-20261119-08"])

    def test_colon_marker_is_a_statement(self):
        self.assert_silent(event("CON-20261119-08\nACC-1: The hook shall deny"))
        self.assert_silent(event("CON-20261119-08\nACC-12 The hook shall deny"))

    def test_letter_after_id_is_not_an_id(self):
        self.assert_silent(event("Package CON-20261119-08x and CON-20261119-08_y"))
        self.assert_denied(event("CON-20261119-08x CON-20261119-09"), ["CON-20261119-09"])
        reason = self.assert_denied(event("CON-20261119-08x CON-20261119-09"), [])
        self.assertNotIn("CON-20261119-08", reason)

    def test_no_id(self):
        self.assert_silent(event("Fix the typo in README.md"))
        self.assert_silent(event("Mention CON-2026-1 and CON-20261119-1234567"))

    def test_other_agent_with_json_sample(self):
        sample = '{"id": "CON-20261119-08", "rows": [1, 2]}'
        self.assert_silent(event(f"Parse this sample data: {sample}", agent="general-purpose"))
        self.assert_silent(event("CON-20261119-08", agent="readback:explorer"))
        self.assert_silent(event("CON-20261119-08", agent=None))

    def test_other_tool(self):
        self.assert_silent(event("CON-20261119-08", tool="Bash"))

    # fail open
    def test_malformed(self):
        for text in ("", "not json", "[]", "null", "{", '{"tool_name": "Agent"}',
                     '{"tool_name": "Agent", "tool_input": "x"}',
                     '{"tool_name": "Agent", "tool_input": {"subagent_type": "readback:worker", "prompt": 5}}',
                     '{"tool_name": "Agent", "tool_input": {"subagent_type": ["a"], "prompt": "CON-20261119-08"}}'):
            with self.subTest(text=text):
                self.assert_silent(text)

    def test_non_utf8_input(self):
        done = subprocess.run([sys.executable, str(HOOK), "claude"], input=b"\xff\xfe{", capture_output=True, timeout=10)
        self.assertEqual((done.returncode, done.stdout), (0, b""))

    def test_exception_gives_empty_output(self):
        code = ("import sys; sys.path.insert(0, %r); import guard_dispatch as g\n"
                "def boom(*a):\n    raise RuntimeError('x')\n"
                "g.find_undelivered = boom\n"
                "sys.exit(g.main())" % str(ROOT / "hooks"))
        done = subprocess.run([sys.executable, "-c", code], input=json.dumps(event("CON-20261119-08")),
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_no_argument(self):
        self.assert_silent(event("ok"), argv=())

    def test_docstring_limits(self):
        text = HOOK.read_text(encoding="utf-8")
        doc = text.split('"""')[1]
        self.assertIn("reads the prompt only", doc)
        self.assertIn("does not show that the worker read the contract", doc)
        self.assertIn("does not run in Codex", doc)


class RegistrationTests(unittest.TestCase):
    def test_hooks_json_entry(self):
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        entries = [e for e in data["hooks"]["PreToolUse"] if e.get("matcher") == "Agent|Task"]
        self.assertEqual(len(entries), 1)
        hooks = entries[0]["hooks"]
        self.assertEqual(hooks, [{"type": "command", "timeout": 10,
                                  "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guard_dispatch.py" claude'}])

    def test_not_portable(self):
        self.assertEqual(list((ROOT / "portable").rglob("guard_dispatch*")), [])


if __name__ == "__main__":
    unittest.main()
