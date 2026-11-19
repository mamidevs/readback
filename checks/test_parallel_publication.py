"""Run two host publishers against the same CURRENT in disposable project homes."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

import test_project_registry as fixtures


ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = {
    "claude": ROOT / "scripts" / "publish_records.py",
    "codex": ROOT / "portable" / "skills" / "readback" / "scripts" / "publish_records.py",
}
EXISTING = (
    "CURRENT   example\n"
    "GOAL      Preserve outstanding work across sessions.\n"
    "IN FORCE  Keep project-wide decisions.\n"
    "OPEN      Older unscoped issue.\n"
    "NEXT      Follow up on the older issue.\n"
)


class ParallelPublicationTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RegistryTests(methodName="runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        self.home = fixture.home
        self.repo = fixture.repo
        self.legacy = False
        temp = fixture.base / "tmp"
        temp.mkdir()
        self.env = {
            "PATH": os.environ["PATH"], "HOME": str(fixture.base), "TMPDIR": str(temp),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        }

    def command(self, host, cwd, task):
        args = [sys.executable, "-B", str(PUBLISHER[host]), "--registry", str(self.fixture.registry),
                "--cwd", str(cwd), "--host", host, "--task", task]
        if self.legacy:
            args.append("--legacy-fallback")
        return args

    def run_cli(self, args):
        return subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=20, check=False)

    def snapshot(self, host, cwd, task):
        result = self.run_cli(self.command(host, cwd, task))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def prepare_write(self, host, cwd, task, sequence, text, snapshot=None, record_id=None):
        if snapshot is None:
            snapshot = self.snapshot(host, cwd, task)
        request = {
            "operation_id": str(uuid.uuid4()),
            "expected_generation": snapshot["expected_generation"],
            "expected_block_sha256": snapshot["expected_block_sha256"],
            "records": [{"id": record_id or f"NOTE-20261119-{sequence:02d}",
                         "text": text, "scope": "shared"}],
            "log_lines": [f"LOG-20261119-{sequence:02d} | example | {text}"],
            "open": "Open " + text,
            "next": "Next " + text,
        }
        path = self.fixture.base / (request["operation_id"] + ".json")
        path.write_text(json.dumps(request))
        return self.command(host, cwd, task) + ["--request", str(path)], request

    def publish_together(self, commands):
        # Start both writer processes before either may exec its real host CLI.
        # The two already-prepared requests describe one shared starting state.
        wait_for_start = """import os
import sys
if sys.stdin.readline() != "go\\n":
    raise SystemExit("missing start signal")
os.execv(sys.executable, sys.argv[1:])
"""
        with ExitStack() as stack:
            writers = []
            for argv in commands:
                writer = stack.enter_context(subprocess.Popen(
                    [sys.executable, "-B", "-c", wait_for_start, *argv], env=self.env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ))
                stack.callback(writer.kill)
                writers.append(writer)
            self.assertTrue(all(writer.poll() is None for writer in writers))
            for writer in writers:
                writer.stdin.write("go\n")
                writer.stdin.flush()
            results = []
            for writer, argv in zip(writers, commands):
                out, err = writer.communicate(timeout=20)
                results.append(subprocess.CompletedProcess(argv, writer.returncode, out, err))
            return results

    def assert_published(self, host, cwd, task, request):
        saved = self.snapshot(host, cwd, task)
        self.assertEqual((saved["open"], saved["next"]), (request["open"], request["next"]))
        self.assertRegex(saved["expected_block_sha256"], r"^[0-9a-f]{64}$")
        record = request["records"][0]
        self.assertTrue((self.home / "records" / (record["id"] + ".md")).read_text().endswith(record["text"]))
        self.assertEqual((self.home / "LOG.md").read_text().count(request["log_lines"][0]), 1)

    def assert_committed_pair(self, pair):
        results = self.publish_together([argv for argv, _request in pair])
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "COMMITTED")

    def test_active_concurrent_sessions_update_independent_tasks_from_same_current(self):
        self.fixture.activate()
        current = self.home / "CURRENT.md"
        current.write_text(EXISTING)
        self.assert_committed_pair([self.prepare_write("claude", self.repo, "catalog", 1, "old catalog"),
                                    self.prepare_write("codex", self.repo, "billing", 2, "old billing")])

        before = current.read_bytes()
        catalog = self.prepare_write("claude", self.repo, "catalog", 3, "catalog audit")
        billing = self.prepare_write("codex", self.repo, "billing", 4, "billing audit")
        self.assertEqual(before, current.read_bytes())
        self.assert_committed_pair([catalog, billing])

        # Read each task from the opposite host, as in a fresh session.
        self.assert_published("codex", self.repo, "catalog", catalog[1])
        self.assert_published("claude", self.repo, "billing", billing[1])
        self.assertIn(EXISTING, current.read_text())
        for request in (catalog[1], billing[1]):
            self.assertIn(request["open"], current.read_text())
            self.assertIn(request["next"], current.read_text())

    def test_two_legacy_sessions_bootstrap_a_fresh_project_home(self):
        self.legacy = True
        fresh = self.fixture.base / "fresh-user"
        fresh.mkdir()
        self.env["HOME"] = str(fresh)
        self.fixture.registry = fresh / "logbook" / ".readback" / "projects.json"
        self.home = fresh / "logbook" / self.repo.name
        self.assertFalse(self.fixture.registry.exists())
        self.assertFalse(self.home.exists())
        first = self.prepare_write("claude", self.repo, "catalog", 1, "catalog setup")
        second = self.prepare_write("codex", self.repo, "orders", 2, "orders setup")
        self.assert_committed_pair([first, second])
        self.assert_published("codex", self.repo, "catalog", first[1])
        self.assert_published("claude", self.repo, "orders", second[1])
        self.assertFalse(self.fixture.registry.exists())

    def test_legacy_linked_worktrees_keep_their_own_task_when_a_branch_disappears(self):
        self.legacy = True
        self.repo = self.fixture.base / "legacy-repo"
        self.fixture.git_init(self.repo)
        self.home = self.fixture.logbook / self.repo.name
        self.home.mkdir()
        current = self.home / "CURRENT.md"
        current.write_text(EXISTING)
        linked = self.fixture.base / "secondary-tree"
        self.fixture.git(self.repo, "worktree", "add", "-q", "-b", "short-lived", str(linked))
        left = self.snapshot("claude", self.repo, "release")
        right = self.snapshot("codex", linked, "release")
        self.assertEqual(left["repository"], right["repository"])
        self.assertNotEqual(left["worktree"], right["worktree"])
        main = self.prepare_write("claude", self.repo, "release", 1, "main release", left)
        other = self.prepare_write("codex", linked, "release", 2, "linked release", right)
        self.assert_committed_pair([main, other])
        self.assert_published("codex", self.repo, "release", main[1])
        self.assert_published("claude", linked, "release", other[1])

        self.fixture.git(self.repo, "worktree", "remove", str(linked))
        self.fixture.git(self.repo, "branch", "-d", "short-lived")
        update, request = self.prepare_write("codex", self.repo, "release", 3, "main follow-up")
        result = self.run_cli(update)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_published("claude", self.repo, "release", request)
        contents = current.read_text()
        for text in (EXISTING, other[1]["open"], other[1]["next"]):
            self.assertIn(text, contents)

    def test_stale_same_task_loser_writes_no_current_record_log_or_journal(self):
        self.fixture.activate()
        (self.home / "CURRENT.md").write_text(EXISTING)
        seed, _ = self.prepare_write("claude", self.repo, "inventory", 1, "starting point")
        result = self.run_cli(seed)
        self.assertEqual(result.returncode, 0, result.stderr)
        requests = [self.prepare_write("claude", self.repo, "inventory", 2, "count crates"),
                    self.prepare_write("codex", self.repo, "inventory", 3, "count pallets")]
        self.assertEqual(requests[0][1]["expected_block_sha256"], requests[1][1]["expected_block_sha256"])
        results = self.publish_together([argv for argv, _ in requests])
        self.assertEqual(sorted(r.returncode for r in results), [0, 2], [r.stderr for r in results])
        for result, (_args, request) in zip(results, requests):
            if result.returncode == 0:
                self.assert_published("codex", self.repo, "inventory", request)
            else:
                self.assertEqual(json.loads(result.stderr)["status"], "CONFLICT")
                self.assertNotIn(request["open"], (self.home / "CURRENT.md").read_text())
                self.assertNotIn(request["log_lines"][0], (self.home / "LOG.md").read_text())
                self.assertFalse((self.home / "records" / (request["records"][0]["id"] + ".md")).exists())
                self.assertFalse((self.home / ".readback" / "publications" /
                                  (request["operation_id"] + ".json")).exists())

    def test_id_collision_between_independent_tasks_does_not_publish_the_loser(self):
        self.fixture.activate()
        requests = [self.prepare_write("claude", self.repo, "catalog", 1, "catalog review"),
                    self.prepare_write("codex", self.repo, "orders", 2, "orders review",
                                       record_id="NOTE-20261119-01")]
        results = self.publish_together([argv for argv, _ in requests])
        self.assertEqual(sorted(r.returncode for r in results), [0, 2], [r.stderr for r in results])
        hosts = ("claude", "codex")
        tasks = ("catalog", "orders")
        winner = next(i for i, result in enumerate(results) if result.returncode == 0)
        loser = 1 - winner
        self.assertEqual(json.loads(results[loser].stderr)["status"], "CONFLICT")
        previous = requests[loser][1]
        self.assertIsNone(self.snapshot(hosts[loser], self.repo, tasks[loser])["expected_block_sha256"])
        self.assertNotIn(previous["log_lines"][0], (self.home / "LOG.md").read_text())

        # Reuse the original task snapshot. Change only the conflicting record ID
        # and the operation UUID; the unsuccessful write never consumed its LOG ID.
        corrected = dict(previous, operation_id=str(uuid.uuid4()), records=[
            dict(previous["records"][0], id="NOTE-20261119-02")
        ])
        path = self.fixture.base / (corrected["operation_id"] + ".json")
        path.write_text(json.dumps(corrected))
        retry = self.run_cli(self.command(hosts[loser], self.repo, tasks[loser]) + ["--request", str(path)])
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assert_published(hosts[loser], self.repo, tasks[loser], corrected)
        self.assert_published(hosts[winner], self.repo, tasks[winner], requests[winner][1])


if __name__ == "__main__":
    unittest.main()
