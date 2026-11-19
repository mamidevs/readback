"""Exercise the two providers' native session metadata using disposable homes."""
import copy
import fcntl
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
import uuid

import test_project_registry as fixtures

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/menu-bar"))
import menu_sessions as sessions
import menu_settings


class ProjectSessionsTests(unittest.TestCase):
    def setUp(self):
        f = fixtures.RegistryTests(methodName="runTest")
        self.addCleanup(f.doCleanups)
        f.setUp()
        f.activate()
        self.fixture = f
        self.legacy = f.base / "repos" / "local-notes"
        f.git_init(self.legacy)
        home = f.logbook / "local-notes"
        home.mkdir()
        (home / "CURRENT.md").touch()
        self.projects = {project["name"]: (project, binding)
                         for project, binding in menu_settings.catalog(f.logbook).values()}
        self.claude = f.base / ".claude"
        self.codex = f.base / ".codex"
        self.codex.mkdir()
        env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex)})
        env.start()
        self.addCleanup(env.stop)
        self.db = sqlite3.connect(self.codex / "state_5.sqlite")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, updated_at INTEGER, archived INTEGER)")
        self.db.commit()

    def thread(self, cwd, updated=1234, archived=0):
        key = str(uuid.uuid4())
        with self.db:
            self.db.execute("INSERT INTO threads VALUES (?,?,?,?)", (key, str(cwd), updated, archived))
        return key

    def listing(self, name):
        return sessions.for_project(*self.projects[name], self.fixture.logbook)

    def test_project_filter_and_native_codex_lock_lifecycle(self):
        key = self.thread(self.fixture.repo)
        self.thread(self.fixture.repo, archived=1)
        other = self.thread(self.legacy)
        lock = self.codex / "thread-writer-locks" / (key + ".lock")
        lock.parent.mkdir()
        with lock.open("wb") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(self.listing("sample-project")["sessions"],
                             [{"id": key, "host": "codex", "status": "running"}])
            self.assertEqual(self.listing("local-notes")["sessions"],
                             [{"id": other, "host": "codex", "status": "recorded"}])
        self.assertEqual(self.listing("sample-project")["sessions"][0]["status"], "recorded")

    def test_external_worktree_follows_registered_host_bindings(self):
        tree = self.fixture.worktree("outside-project")
        key = self.thread(tree)
        self.assertEqual(self.listing("sample-project")["sessions"], [])
        updated = copy.deepcopy(self.fixture.data)
        updated["projects"][0]["repositories"][0]["worktrees"] = [
            {"root": str(tree), "identity": self.fixture.identity(tree)}]
        self.fixture.activate(updated)
        self.assertEqual(self.listing("sample-project")["sessions"],
                         [{"id": key, "host": "codex", "status": "recorded"}])

    def test_claude_birth_and_worktree_resolution(self):
        worktree = self.fixture.base / "unrelated-folder"
        self.fixture.git(self.legacy, "worktree", "add", "--detach", str(worktree), "HEAD")
        key = str(uuid.uuid4())
        pid = 497
        started = "Wed Nov 18 09:30:02 2026"
        record = {"sessionId": key, "pid": pid, "procStart": started, "cwd": str(worktree)}
        path = self.claude / "sessions/497.json"
        fixtures.write_json(path, record)
        self.assertEqual(self.listing("sample-project")["sessions"], [])
        # Stub just the scoped process-start probe, leaving all Git resolution real.
        original = sessions.subprocess.run
        def probe(argv, *args, **kwargs):
            if argv[0] == "/bin/ps":
                self.assertEqual(kwargs["env"]["TZ"], "UTC")
                return sessions.subprocess.CompletedProcess(argv, 0, started + "\n")
            return original(argv, *args, **kwargs)
        with patch.object(sessions.subprocess, "run", side_effect=probe):
            self.assertEqual(self.listing("local-notes")["sessions"],
                             [{"id": key, "host": "claude", "status": "running"}])
            fixtures.write_json(path, dict(record, procStart="Yesterday"))
            self.assertEqual(self.listing("local-notes")["sessions"][0]["status"], "recorded")

    def test_limits_apply_to_selected_project_and_active_sorts_first(self):
        quiet = self.thread(self.legacy, updated=1)
        lock = self.codex / "thread-writer-locks" / (quiet + ".lock")
        lock.parent.mkdir()
        with lock.open("wb") as fd, patch.object(sessions, "DISPLAY_LIMIT", 2):
            fcntl.flock(fd, fcntl.LOCK_EX)
            for i in range(7):
                self.thread(self.legacy, updated=10 + i)
                self.thread(self.fixture.repo, updated=500 + i)
            result = self.listing("local-notes")
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["sessions"]), 2)
        self.assertEqual(result["sessions"][0]["id"], quiet)
        self.assertEqual(result["sessions"][0]["status"], "running")

    def test_invalid_sources_are_distinguished_from_empty(self):
        self.assertEqual(self.listing("local-notes"), {"sessions": [], "truncated": False, "unavailable": []})
        with self.db:
            self.db.execute("DROP TABLE threads")
        path = self.claude / "sessions/108.json"
        path.parent.mkdir(parents=True)
        path.write_text("not JSON")
        key = str(uuid.uuid4())
        fixtures.write_json(path.with_name("109.json"), {"sessionId": key, "cwd": str(self.legacy)})
        result = self.listing("local-notes")
        self.assertEqual(set(result["unavailable"]), {"codex", "claude"})
        self.assertEqual(result["sessions"], [{"id": key, "host": "claude", "status": "recorded"}])


if __name__ == "__main__":
    unittest.main()
