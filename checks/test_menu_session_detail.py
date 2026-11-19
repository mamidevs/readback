"""Exercise the session-detail bridge against disposable provider transcripts."""
import json
from pathlib import Path
import re
import shutil
import sqlite3
import unittest
import uuid

import test_menu_settings as settings
import test_project_registry as fixtures

TIME = "2026-11-19T12:00:00.000Z"
EMPTY = {"available": False, "events": [], "model": None}
SECRET = "private-test-marker"


def append(path, *rows):
    path.parent.mkdir(exist_ok=True, parents=True)
    with path.open("a") as file:
        for row in rows:
            file.write(json.dumps(row) + "\n")


class SessionDetailTests(unittest.TestCase):
    def setUp(self):
        harness = settings.MenuSettingsTests(methodName="runTest")
        self.addCleanup(harness.doCleanups)
        harness.setUp()
        self.harness = harness
        self.fixture = harness.fixture
        self.fixture.activate()
        for name in ("menu_sessions.py", "menu_activity.py"):
            shutil.copyfile(settings.ROOT / "app/menu-bar" / name, harness.bridge.parent / name)
        self.projects = {row["name"]: row["id"] for row in harness.projects()}
        self.claude = self.fixture.base / ".claude"
        self.codex = self.fixture.base / ".codex"
        self.codex.mkdir()
        harness.env.update(CODEX_HOME=str(self.codex), CLAUDE_CONFIG_DIR=str(self.claude))
        self.key = str(uuid.uuid4())
        self.cwd = str(self.fixture.repo)

    def detail(self, host, project="sample-project", key=None):
        return self.harness.command("session-detail", self.projects[project], host, key or self.key)["detail"]

    def codex_log(self):
        log = self.codex / "sessions/2026/11/19" / ("rollout-2026-11-19T12-00-00-" + self.key + ".jsonl")
        with sqlite3.connect(self.codex / "state_5.sqlite") as db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, archived INTEGER, rollout_path TEXT, model TEXT)")
            db.execute("INSERT INTO threads VALUES (?,?,0,?,?)", (self.key, self.cwd, str(log), "gpt-6.2-sol"))
        append(log, {"type": "session_meta", "payload": {"id": self.key, "cwd": self.cwd}})
        return log

    def claude_log(self):
        fixtures.write_json(self.claude / "sessions/1234.json", {"sessionId": self.key, "cwd": self.cwd})
        encoded = re.sub(r"[^a-zA-Z0-9]", "-", self.cwd)
        return self.claude / "projects" / encoded / (self.key + ".jsonl")

    def test_codex_cli_reports_latest_completed_and_requested_actions(self):
        log = self.codex_log()
        append(log, {"type": "turn_context", "payload": {"model": "gpt-6.2-astra"}},
               {"type": "response_item", "timestamp": TIME, "payload": {
                   "type": "function_call", "name": "exec_command",
                   "arguments": json.dumps({"cmd": "pytest -q " + SECRET})}},
               {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                   "input": "text(await tools.web__run({query: '" + SECRET + "'}))"}},
               {"type": "response_item", "payload": {"type": "function_call_output", "output": SECRET}},
               {"type": "event_msg", "timestamp": TIME, "payload": {"type": "task_complete", "last_agent_message": SECRET}},
               {"type": "response_item", "payload": {"type": "reasoning", "summary": SECRET}})
        detail = self.detail("codex")
        self.assertEqual(detail["model"], "gpt-6.2-astra")
        self.assertEqual([item["label"] for item in detail["events"]],
                         ["İşlem turu tamamlandı", "Araç sonucu alındı", "Web araştırması isteği", "Test çalıştırma isteği"])
        self.assertTrue(detail["available"])
        self.assertEqual(detail["events"][0]["timestamp"], TIME)
        self.assertNotIn(SECRET, json.dumps(detail))
        self.assertEqual(self.detail("codex", "legacy"), EMPTY)

    def test_claude_updates_selected_activity_ignoring_private_content_and_other_sessions(self):
        log = self.claude_log()
        row = {"type": "assistant", "sessionId": self.key, "cwd": self.cwd, "timestamp": TIME}
        append(log, dict(row, message={"model": "claude-sonnet-4-6", "content": [
            {"type": "thinking", "thinking": SECRET}, {"type": "tool_use", "name": "Read", "input": {"file_path": SECRET}}]}))
        self.assertEqual(self.detail("claude"), {"model": "claude-sonnet-4-6", "available": True,
                                                 "events": [{"label": "Dosya okuma isteği", "timestamp": TIME}]})
        append(log, dict(row, type="user", message={"content": [{"type": "tool_result", "is_error": True, "content": SECRET}]}),
               dict(row, sessionId=str(uuid.uuid4()), message={"content": [{"type": "text", "text": SECRET}]}),
               dict(row, cwd="/foreign", message={"content": [{"type": "text", "text": SECRET}]}))
        detail = self.detail("claude")
        self.assertEqual([e["label"] for e in detail["events"]], ["Araç hata bildirdi", "Dosya okuma isteği"])
        self.assertNotIn(SECRET, json.dumps(detail))
        self.assertEqual(self.detail("claude", "legacy"), EMPTY)
        self.assertEqual(self.detail("claude", key="../../something"), EMPTY)

    def test_codex_rejects_symlinks_escape_header_mismatch_and_missing_records(self):
        log = self.codex_log()
        original = log.read_bytes()
        foreign = self.fixture.base / "external" / log.name
        foreign.parent.mkdir()
        log.rename(foreign)
        self.assertEqual(self.detail("codex"), EMPTY)
        log.symlink_to(foreign)
        self.assertEqual(self.detail("codex"), EMPTY)
        log.unlink()
        append(log, {"type": "session_meta", "payload": {"id": str(uuid.uuid4()), "cwd": self.cwd}})
        self.assertEqual(self.detail("codex"), EMPTY)
        log.write_bytes(original)
        with sqlite3.connect(self.codex / "state_5.sqlite") as db:
            db.execute("UPDATE threads SET rollout_path=?", (str(foreign),))
        self.assertEqual(self.detail("codex"), EMPTY)

    def test_only_complete_recent_records_contribute_to_activity(self):
        log = self.codex_log()
        append(log, {"type": "event_msg", "payload": {"type": "error"}},
               {"type": "response_item", "payload": {"type": "function_call_output", "output": "A" * (512 * 1024)}})
        append(log, *({"type": "event_msg", "timestamp": TIME, "payload": {"type": "task_complete"}} for _ in range(16)))
        with log.open("ab") as file:
            file.write(b'{"type":"event_msg","payload":{"type":"error"}}')
        detail = self.detail("codex")
        self.assertEqual(detail["events"], [{"label": "İşlem turu tamamlandı", "timestamp": TIME}] * 12)
        self.assertEqual(detail["model"], "gpt-6.2-sol")


if __name__ == "__main__":
    unittest.main()
