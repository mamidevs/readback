#!/usr/bin/env python3
"""Offline checks for the plugin hook that denies Git commands which discard work.

Run: python3 -m unittest discover -s checks -p 'test_guard_git.py' -v
"""

import json
from pathlib import Path
import subprocess
import sys
import unittest


HOOK = Path(__file__).resolve().parents[1] / "hooks" / "guard_git.py"
CONFIG = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"

DENIED = [
    "git reset --hard",
    "git -C repo reset --hard HEAD~1",
    "cd work && git stash",
    "git stash -u",
    "git stash push -m keep",
    "git stash drop",
    "git stash pop",
    "git checkout -- greet.sh",
    "git checkout .",
    "git checkout -f main",
    "git restore greet.sh",
    "git restore --staged --worktree greet.sh",
    "git restore -SW greet.sh",
    "git clean -fd",
    "FOO=1 git clean -fdx",
    "bash -c 'git reset --hard'",
    "sh -lc \"git stash\"",
    "echo $(git stash)",
    "echo `git stash`",
    "git switch --discard-changes main",
    "/usr/bin/git reset --hard",
    "git --no-pager -c core.pager=cat stash pop",
    "sudo git clean -f",
    "ls\ngit reset --hard",
    "git stash 'unbalanced",
]

ALLOWED = [
    "git status",
    "git stash list",
    "git stash show -p",
    "git reset HEAD greet.sh",
    "git reset --soft HEAD~1",
    "git checkout main",
    "git checkout -b feature",
    "git restore --staged greet.sh",
    "git clean -n",
    "git clean --dry-run -d",
    "git switch main",
    "echo 'git reset --hard'",
    "grep -n 'git stash' README.md",
    "python3 -m unittest",
]


def run(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOK)], input=text, capture_output=True, text=True, timeout=10)


def bash(command, agent_type=None):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    if agent_type:
        payload["agent_type"] = agent_type
    return run(payload)


class GuardGitTests(unittest.TestCase):
    def assert_denied(self, result, command):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), "allowed: " + repr(command))
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("readback", decision["permissionDecisionReason"])

    def test_commands_that_discard_work_are_denied(self):
        for command in DENIED:
            with self.subTest(command=command):
                self.assert_denied(bash(command), command)

    def test_other_commands_are_allowed(self):
        for command in ALLOWED:
            with self.subTest(command=command):
                result = bash(command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "", "denied: " + repr(command))

    def test_sub_agent_is_told_to_return_blocked(self):
        result = bash("git reset --hard", agent_type="readback:worker")
        reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("BLOCKED", reason)
        self.assertIn("git reset --hard", reason)

    def test_other_tools_and_bad_input_are_allowed(self):
        for payload in ({"tool_name": "Read", "tool_input": {"file_path": "x"}}, "not json", "{}"):
            with self.subTest(payload=payload):
                result = run(payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_plugin_config_runs_the_hook_before_bash(self):
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        entries = config["hooks"]["PreToolUse"]
        self.assertEqual([entry["matcher"] for entry in entries], ["Bash"])
        command = entries[0]["hooks"][0]["command"]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/guard_git.py", command)


if __name__ == "__main__":
    unittest.main()
