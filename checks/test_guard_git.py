#!/usr/bin/env python3
"""Offline checks for the plugin hook that denies Git commands which discard work.

Run: python3 -m unittest discover -s checks -p 'test_guard_git.py' -v
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "guard_git.py"
CONFIG = ROOT / "hooks" / "hooks.json"
PORTABLE_HOOK = ROOT / "portable" / "skills" / "readback" / "scripts" / "guard_git.py"
CODEX_CONFIG = ROOT / "portable" / "codex" / "hooks.json"

# PreToolUse input that Codex 0.155.1 sent on 2026-11-17, with ids shortened.
CODEX_LEAD = {
    "session_id": "01a1e955-cccf", "turn_id": "01a1e955-cd33", "transcript_path": None,
    "cwd": ".", "hook_event_name": "PreToolUse", "model": "gpt-6-sol",
    "permission_mode": "bypassPermissions", "tool_name": "Bash",
    "tool_input": {"command": "git reset --hard HEAD"}, "tool_use_id": "exec-73f3748a",
}
CODEX_SUB_AGENT = dict(CODEX_LEAD, agent_id="01a1e955-ed3e", agent_type="default")

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
    "git checkout HEAD tracked.txt",
    "git checkout HEAD~1 src/app.py docs",
    "git restore greet.sh",
    "git restore --staged --worktree greet.sh",
    "git restore -SW greet.sh",
    "git clean -fd",
    "git rm -f notes.txt",
    "git rm -rf build",
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
    "git stash create",
    "git stash store deadbeef",
    "git checkout -qf main",
    "git switch -qf main",
    "git reset --har",
    "git checkout --forc main",
    "git switch --discard main",
    "git checkout --pathspec-from-file=list.txt",
    "git rm -qf notes.txt",
    "git restore --stag --work greet.sh",
    "env -u HOME git reset --hard",
    "timeout 5 git stash",
    "nice -n 10 git clean -f",
    "xargs git stash drop",
    "$(which git) stash",
    "eval \"git stash\"",
    "(cd work; git stash)",
    "git clean -f -- -n",
    "git restore -- -S",
    "echo \"$(git reset --hard)\"",
    "echo \"`git stash`\"",
    "x=\"$(echo a; git clean -fd)\"",
]

ALLOWED = [
    "git status",
    "git stash list",
    "git stash show -p",
    "git reset HEAD greet.sh",
    "git reset --soft HEAD~1",
    "git checkout -b feature",
    "git checkout -b feature main",
    "git rm --cached notes.txt",
    "git rm notes.txt",
    "git restore --staged greet.sh",
    "git clean -n",
    "git clean --dry-run -d",
    "git switch main",
    "(git status)",
    "echo 'git reset --hard'",
    "grep -n 'git stash' README.md",
    "python3 -m unittest",
    "git clean -nfd",
    "git clean --dry -d",
    "git restore --stag greet.sh",
    "git checkout -qb feature",
    "git switch -c feature",
    "echo done && git status",
    "git rm -- -f",
    "git reset -- --hard",
    "git clean -n -- -f",
]


class GuardGitTests(unittest.TestCase):
    hook = HOOK

    def run_hook(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, str(self.hook)], input=text, capture_output=True, text=True, timeout=10)

    def bash(self, command, agent_type=None, cwd=None):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
        if cwd:
            payload["cwd"] = str(cwd)
        if agent_type:
            payload["agent_type"] = agent_type
        return self.run_hook(payload)

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
                self.assert_denied(self.bash(command), command)

    def test_other_commands_are_allowed(self):
        for command in ALLOWED:
            with self.subTest(command=command):
                result = self.bash(command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "", "denied: " + repr(command))

    def test_checkout_of_an_existing_path_is_denied(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "tracked.txt").write_text("x\n")
            (base / "deleted.txt").write_text("x\n")
            git = ["git", "-C", str(base), "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
            subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
            subprocess.run(git + ["add", "."], check=True)
            subprocess.run(git + ["commit", "-q", "-m", "base"], check=True)
            (base / "deleted.txt").unlink()
            (base / "sub").mkdir()
            (base / "sub" / "file.txt").write_text("x\n")
            for command in ("git checkout tracked.txt", "git -C sub checkout file.txt",
                            "git --work-tree=sub checkout file.txt", "git checkout deleted.txt"):
                with self.subTest(command=command):
                    self.assert_denied(self.bash(command, cwd=base), command)
            for command in ("git checkout main", "git checkout HEAD~0", "git checkout -b feature main"):
                with self.subTest(command=command):
                    self.assertEqual(self.bash(command, cwd=base).stdout, "", "a revision was denied: " + command)

    def test_sub_agent_is_told_to_return_blocked(self):
        result = self.bash("git reset --hard", agent_type="readback:worker")
        reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("BLOCKED", reason)
        self.assertIn("git reset --hard", reason)

    def test_other_tools_and_bad_input_are_allowed(self):
        for payload in ({"tool_name": "Read", "tool_input": {"file_path": "x"}}, "not json", "{}"):
            with self.subTest(payload=payload):
                result = self.run_hook(payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_codex_input_is_denied(self):
        result = self.run_hook(CODEX_LEAD)
        self.assert_denied(result, "codex lead")
        self.assertIn("Tell the owner", json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_known_gap_session_folder_outside_the_repository(self):
        # Codex passes the session folder, not the workdir of the command.
        # Outside a repository a single missing name counts as a revision, so the hook allows it.
        with tempfile.TemporaryDirectory() as folder:
            result = self.bash("git checkout tracked.txt", cwd=folder)
            self.assertEqual(result.stdout, "", "the documented gap changed; update INSTALL.md")

    def test_codex_sub_agent_is_told_to_return_blocked(self):
        result = self.run_hook(CODEX_SUB_AGENT)
        self.assert_denied(result, "codex sub-agent")
        self.assertIn("BLOCKED", json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_plugin_config_runs_the_hook_before_bash(self):
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        entries = config["hooks"]["PreToolUse"]
        self.assertEqual([entry["matcher"] for entry in entries], ["Bash"])
        command = entries[0]["hooks"][0]["command"]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/guard_git.py", command)


class PortableGuardGitTests(GuardGitTests):
    """The copy that the Codex hook runs passes the same checks."""
    hook = PORTABLE_HOOK

    def test_portable_copy_equals_the_plugin_hook(self):
        self.assertEqual(PORTABLE_HOOK.read_bytes(), HOOK.read_bytes())

    def test_codex_config_runs_the_installed_copy_before_bash(self):
        config = json.loads(CODEX_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(list(config["hooks"]), ["PreToolUse", "UserPromptSubmit", "SessionStart", "SessionEnd"])
        entries = config["hooks"]["PreToolUse"]
        self.assertEqual([entry["matcher"] for entry in entries], ["Bash"])
        handler = entries[0]["hooks"][0]
        self.assertEqual(handler["type"], "command")
        self.assertIn('"$HOME/.agents/skills/readback/scripts/guard_git.py"', handler["command"])


if __name__ == "__main__":
    unittest.main()
