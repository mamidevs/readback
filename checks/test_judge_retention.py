"""Repository-scoped retention against disposable Git repositories and round ledgers."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import judge_rounds as rounds
import readback_settings
import review_receipt as receipts
import review_retention as retention


class Retention(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        env = patch.dict(os.environ, HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.project = readback_settings.resolve(str(self.repo), "codex")
        self.common = Path(self.project["git_common_dir"])
        self.rounds = self.home / "logbook" / ".readback" / "rounds"
        self.now = int(time.time())
        self.cutoff = self.now - retention.RETENTION_DAYS * retention.DAY
        self.old = self.cutoff - 1
        self.capture = {"state": "ok", "manifest_sha256": "e" * 64, "reason": None}

    def age(self, files, stamp=None):
        value = self.old if stamp is None else stamp
        for file in files:
            os.utime(file, (value, value))
        return files

    def date(self, stamp):
        return time.strftime(rounds.FORMAT, time.gmtime(stamp))

    def receipt(self, worktree=None):
        started = receipts.started_record(
            uuid.uuid4().hex, "b" * 64, self.date(self.old), {"worktree_root": str(worktree or self.repo)},
            self.capture, "/contract", "1" * 64, "/handoff", "2" * 64, "3" * 64,
            {"model": "test"}, {"file_sha256": "4" * 64})
        first = receipts.store_started(self.common, started)
        complete = receipts.completed_record(started, self.capture, "ends-equal", {"valid": True},
                                            {"verdict": "PASS"}, None, 0, [])
        second, marker = receipts.store_completed(self.common, complete)
        return self.age(tuple(map(Path, (first, second, marker))))

    def skipped(self, worktree=None):
        record = receipts.no_review_record(
            uuid.uuid4().hex, self.date(self.old), "off", {"worktree_root": str(worktree or self.repo)},
            "/contract", "1" * 64, "/handoff", "2" * 64, {}, {"mode": "off"}, {"file_sha256": "4" * 64})
        return self.age([Path(receipts.store_no_review(self.common, record))])[0]

    def ledger(self, repository=None, home=None, date=None, tier="gate"):
        home = home or self.project["home"]
        repo = repository or self.project["repository"]
        lineage = "CON-20261119-07"
        folder, path, lock = map(Path, rounds.paths(home, repo, lineage, tier))
        folder.mkdir(parents=True, exist_ok=True)
        lock.touch()
        ledger = rounds.new_ledger(home, repo, lineage, tier, "x")
        payloads = []
        for result in ("FAIL", None):
            attempt = uuid.uuid4().hex
            ledger["attempts"].append({
                "attempt_id": attempt, "reserved_at": self.date(self.old), "deadline_seconds": 300,
                "settings_digest": "x", "route": {}, "authorization": None, "result": result,
                "finished_at": (date or self.date(self.old)) if result else None,
                "ended_by": "helper" if result else None})
            payload = folder / path.stem / attempt
            payload.mkdir(parents=True)
            for name in ("handoff.txt", "observation.txt", "answer.txt", "owner.txt"):
                (payload / name).write_text("sensitive fixture")
            self.age(payload.iterdir())
            payloads.append(payload)
        rounds.publish(str(folder), str(path), ledger)
        return path, lock, *payloads

    def run_cli(self, *flags):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(retention.time, "time", return_value=self.now):
            code = retention.main(["--host", "codex", *flags, str(self.repo)])
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_cli_preview_apply_and_repeat(self):
        expired = self.age(self.receipt(), self.cutoff)
        recent = self.receipt()
        self.age([recent[1]], self.cutoff + 1)
        pending, done, marker = self.receipt()
        done.unlink()
        skipped = self.skipped()
        fresh_skip = self.skipped()
        self.age([fresh_skip], self.now)
        future_skip = self.skipped()
        record = json.loads(future_skip.read_bytes())
        record["created"] = self.date(self.now)
        future_skip.write_text(json.dumps(record))
        self.age([future_skip])
        ledger, lock, finished, active = self.ledger()
        saved = ledger.read_bytes()
        preview = self.run_cli()
        self.assertEqual(preview, dict(mode="preview", receipts=3, markers=1, round_payloads=3, retention_days=30))
        self.assertTrue(all(file.exists() for file in (*expired, skipped, finished / "answer.txt")))
        self.assertEqual(self.run_cli("--apply"), {**preview, "mode": "apply"})
        self.assertFalse(any(file.exists() for file in (*expired, skipped, finished / "answer.txt",
                                                        finished / "observation.txt", finished / "handoff.txt")))
        self.assertTrue(all(file.exists() for file in (*recent, pending, marker, fresh_skip, future_skip,
                                                       finished / "owner.txt", lock)))
        self.assertEqual(len(list(active.iterdir())), 4)
        self.assertEqual(ledger.read_bytes(), saved)
        self.assertEqual(self.run_cli("--apply")["receipts"], 0)

    def test_foreign_unknown_and_malformed_artifacts_survive(self):
        foreign_receipt = self.receipt(self.home / "other")
        foreign_skip = self.skipped(self.home / "other")
        foreign = self.ledger(repository="other")[2]
        foreign_home = self.ledger(home="other home")[2]
        recent = self.ledger(date=self.date(self.cutoff + 1))[2]
        valid, done, marker = self.receipt()
        record = json.loads(done.read_bytes())
        record["attempt_id"] = "wrong"
        done.write_text(json.dumps(record))
        self.age([done])
        # An old payload with a right-shaped path needs a matching valid ledger.
        misplaced = self.rounds / ("9" * 64 + ".json")
        misplaced.write_bytes(next(self.rounds.glob("*.json")).read_bytes())
        wrong_folder = self.rounds / misplaced.stem / foreign.name
        wrong_folder.mkdir(parents=True)
        unrelated = wrong_folder / "answer.txt"
        unrelated.write_text("keep")
        self.age([unrelated])
        invalid = self.skipped()
        invalid.write_text("{partial")
        self.age([invalid])
        self.assertEqual(self.run_cli("--apply"), dict(mode="apply", receipts=0, markers=0,
                                                     round_payloads=0, retention_days=30))
        self.assertTrue(all(p.exists() for p in (*foreign_receipt, foreign_skip, valid, done, marker,
                                                 invalid, unrelated, foreign / "handoff.txt",
                                                 foreign_home / "answer.txt", recent / "answer.txt")))

    def test_historical_receipts_from_linked_worktree_expire_from_main(self):
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                        "commit", "--allow-empty", "-qm", "base"], check=True)
        worktree = self.home / "linked"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "--detach", str(worktree)],
                       check=True)
        files = self.receipt(worktree)
        self.assertEqual(self.run_cli("--apply")["receipts"], 2)
        self.assertFalse(any(p.exists() for p in files))

    def test_direct_apply_accepts_removed_worktree_with_common_identity_only(self):
        selected = self.skipped(self.home / "removed")
        foreign = self.skipped()
        for path, common in ((selected, self.common), (foreign, self.home / "other.git")):
            data = json.loads(path.read_bytes())
            data["repository"]["git_common_dir_sha256"] = hashlib.sha256(os.fsencode(common)).hexdigest()
            path.write_text(json.dumps(data))
            os.utime(path, (1, self.old))  # First read can update access time.
        self.assertEqual(self.run_cli("--apply")["receipts"], 1)
        self.assertFalse(selected.exists())
        self.assertTrue(foreign.exists())

    def test_symlinked_store_and_payloads_survive(self):
        first, done, marker = self.receipt()
        real = self.common / "readback"
        moved = self.home / "external"
        real.rename(moved)
        real.symlink_to(moved, target_is_directory=True)
        _, _, finished, _ = self.ledger()
        target = self.home / "untouched"
        target.write_text("keep")
        self.age([target])
        (finished / "answer.txt").unlink()
        (finished / "answer.txt").symlink_to(target)
        for name in ("handoff.txt", "observation.txt"):
            self.age([finished / name], self.now)
        self.assertEqual(self.run_cli("--apply")["round_payloads"], 0)
        self.assertTrue(all(p.exists() for p in (first, done, marker, target)))
        self.assertTrue((finished / "answer.txt").is_symlink())
        real.unlink()
        moved.rename(real)
        review_folder = first.parent
        relocated = self.home / "relocated"
        review_folder.rename(relocated)
        review_folder.symlink_to(relocated, target_is_directory=True)
        finished.rename(self.home / "round-data")
        finished.symlink_to(self.home / "round-data", target_is_directory=True)
        self.assertEqual(self.run_cli("--apply")["receipts"], 0)
        self.assertTrue(first.exists())
        self.assertTrue((finished / "observation.txt").exists())

    def test_invalid_ledger_and_its_symlink_cannot_authorize_deletion(self):
        ledger, _, folder, _ = self.ledger()
        other = self.home / "ledger-backup"
        ledger.rename(other)
        ledger.symlink_to(other)
        (self.rounds / ("d" * 64 + ".json")).mkdir()
        self.assertEqual(self.run_cli("--apply")["round_payloads"], 0)
        self.assertTrue((folder / "answer.txt").exists())
        ledger.unlink()
        data = json.loads(other.read_bytes())
        data["attempts"].append(data["attempts"][0])
        ledger.write_text(json.dumps(data))
        self.assertEqual(self.run_cli("--apply")["round_payloads"], 0)
        self.assertTrue((folder / "handoff.txt").exists())

    def test_interrupted_cleanup_resumes_and_shared_marker_siblings_survive(self):
        first, done, marker = self.receipt()
        first.unlink()
        marker.unlink()
        other = self.receipt()
        self.age([other[1]], self.now)
        self.assertEqual(self.run_cli("--apply")["receipts"], 1)
        self.assertFalse(done.exists())
        self.assertTrue(all(p.exists() for p in other))

    def test_rounds_only_daily_clock_leaves_foreign_and_accounting_folders_alone(self):
        self.ledger(repository="foreign")
        book = self.rounds.parent
        before = set(self.rounds.iterdir())
        retention.maybe_prune(self.project)
        self.assertEqual(set(book.iterdir()), {self.rounds})
        self.assertEqual(set(self.rounds.iterdir()), before)
        _, _, finished, _ = self.ledger()
        before = set(self.rounds.iterdir())
        retention.maybe_prune(self.project)
        self.assertFalse((finished / "answer.txt").exists())
        self.assertEqual(set(self.rounds.iterdir()), before)
        self.assertEqual(len(list(book.glob(".retention-*"))), 1)
        with patch.object(retention.Sweep, "rounds", side_effect=AssertionError("rescanned")):
            retention.maybe_prune(self.project)

    def test_daily_skips_without_stores_and_between_sweeps(self):
        retention.maybe_prune(self.project)
        self.assertFalse((self.common / "readback").exists())
        self.assertFalse(self.rounds.exists())
        old = self.skipped()
        with patch.object(retention.time, "time", return_value=self.now):
            retention.maybe_prune(self.project)
            self.assertFalse(old.exists())
            self.assertFalse(list((self.common / "readback").glob(".retention-*")))
            self.assertEqual(len(list(self.rounds.parent.glob(".retention-*"))), 1)
            late = self.skipped()
            with patch.object(retention.Sweep, "receipts", side_effect=AssertionError("rescanned")):
                retention.maybe_prune(self.project)
            self.assertTrue(late.exists())
        with patch.object(retention.time, "time", return_value=self.now + retention.DAY):
            retention.maybe_prune(self.project)
        self.assertFalse(late.exists())


if __name__ == "__main__":
    unittest.main()
