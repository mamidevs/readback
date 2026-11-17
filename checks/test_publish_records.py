#!/usr/bin/env python3
"""Exercise publication conflicts and recovery in disposable registered projects.

No provider, network, private logbook or application repository is contacted.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "checks"))
import project_registry as registry
import publish_records as publisher
import test_project_registry as fixtures


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RegistryTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.activate()
        self.base = self.fixture.base
        self.home = self.fixture.home
        self.repo = self.fixture.repo
        self.registry = self.fixture.registry
        self.project = self.fixture.project
        self.generation = self.fixture.data["generation"]
        self.record_dir = self.home / "records"
        self.journals = self.home / ".readback" / "publications"

    def request(self, number=1, **overrides):
        current = self.home / "CURRENT.md"
        result = {
            "operation_id": str(uuid.uuid4()),
            "expected_generation": self.generation,
            "expected_current_sha256": digest(current.read_bytes()) if current.exists() else None,
            "records": [{"id": f"CON-20261117-{number:02d}", "text": "Synthetic contract.\n", "scope": "shared"},
                        {"id": f"HND-20261117-{number:02d}", "text": "Synthetic handoff.\n",
                         "scope": [self.project["repositories"][0]["repo_uuid"]]}],
            "log_lines": [f"LOG-20261117-{number:02d} | sample-project | Synthetic publication."],
            "current": f"CURRENT   sample-project\nupdated   2026-11-17 | after LOG-20261117-{number:02d}\n",
        }
        result.update(overrides)
        return result

    def publish(self, request, host="codex", **kwargs):
        return publisher.publish(self.registry, self.repo, host, request, **kwargs)

    def expect_error(self, request, code=None, **kwargs):
        with self.assertRaises((registry.RegistryError, OSError)) as caught:
            self.publish(request, **kwargs)
        if code:
            self.assertEqual(getattr(caught.exception, "code", None), code)
        return caught.exception

    def state_files(self):
        """Lock/empty-directory creation is allowed; compare persisted publication data."""
        if not self.home.exists():
            return {}
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in self.home.rglob("*") if p.is_file() and not p.is_symlink()}

    def process_args(self, request, name):
        request_file = self.base / (name + ".json")
        request_file.write_text(json.dumps(request))
        return [sys.executable, str(ROOT / "scripts" / "publish_records.py"),
                "--registry", str(self.registry), "--cwd", str(self.repo),
                "--host", "codex", "--request", str(request_file)]

    def test_normal_publication_preserves_scope_provenance_and_prior_log(self):
        before = b"LOG-20261116-01 | Existing history without newline"
        (self.home / "LOG.md").write_bytes(before)
        request = self.request()
        result = self.publish(request)
        self.assertEqual(result["status"], "COMMITTED")
        self.assertFalse(result["replayed"])
        self.assertTrue((self.home / "LOG.md").read_bytes().startswith(before + b"\n"))
        self.assertEqual((self.home / "CURRENT.md").read_text(), request["current"])
        for record in request["records"]:
            text = (self.record_dir / (record["id"] + ".md")).read_text()
            self.assertIn("project=" + self.project["project_uuid"], text)
            self.assertIn("scope=" + json.dumps(record["scope"], separators=(",", ":")), text)
            self.assertTrue(text.endswith(record["text"]))
        journal = json.loads((self.journals / (request["operation_id"] + ".json")).read_text())
        self.assertEqual(journal["provenance"]["repo_uuid"], self.project["repositories"][0]["repo_uuid"])
        self.assertEqual(journal["provenance"]["cwd"], str(self.repo))
        self.assertEqual(journal["provenance"]["worktree_root"], str(self.repo))
        self.assertEqual(journal["state"], "COMMITTED")

    def test_committed_replay_is_idempotent_across_hosts(self):
        request = self.request()
        self.publish(request)
        before = self.state_files()
        response = self.publish(request, host="claude")
        self.assertTrue(response["replayed"])
        self.assertEqual(before, self.state_files())

    def test_new_publication_never_overwrites_existing_record_even_identical_text(self):
        request = self.request()
        self.publish(request)
        next_request = self.request(number=2)
        next_request["records"][0] = copy.deepcopy(request["records"][0])
        before = self.state_files()
        self.expect_error(next_request, "CONFLICT")
        self.assertEqual(before, self.state_files())

    def test_stale_current_rejected_without_publication_data(self):
        request = self.request()
        (self.home / "CURRENT.md").write_text("A concurrent lead changed the current state.\n")
        before = self.state_files()
        self.expect_error(request, "CONFLICT")
        self.assertEqual(before, self.state_files())

    def test_stale_generation_rejected_without_publication_data(self):
        request = self.request(expected_generation=self.generation - 1)
        before = self.state_files()
        self.expect_error(request, "CONFLICT")
        self.assertEqual(before, self.state_files())

    def test_operation_uuid_cannot_replay_changed_payload(self):
        request = self.request()
        self.publish(request)
        changed = copy.deepcopy(request)
        changed["current"] += "Unexpected change.\n"
        before = self.state_files()
        self.expect_error(changed, "CONFLICT")
        self.assertEqual(before, self.state_files())

    def test_invalid_paths_scopes_ids_and_multiline_log_rejected_before_data(self):
        cases = []
        for invalid in ["../escape", "/tmp/escape", "HND-20261117-01/escape", "HND-20261117-01.md"]:
            request = self.request(); request["records"][0]["id"] = invalid; cases.append(request)
        for invalid in [[], [str(uuid.uuid4())], "unclassified", None]:
            request = self.request(); request["records"][0]["scope"] = invalid; cases.append(request)
        request = self.request(); request["records"].append(copy.deepcopy(request["records"][0])); cases.append(request)
        cases.extend([self.request(current="line\n" * 31), self.request(log_lines=["LOG-20261117-01\nDEC-injected"]),
                      self.request(expected_generation=True), self.request(operation_id="../../escaped")])
        before = self.state_files()
        for request in cases:
            with self.subTest(request=request):
                self.expect_error(request)
                self.assertEqual(before, self.state_files())

    def test_each_documented_interruption_retries_without_duplicates(self):
        for number, stage in enumerate(["journal", "records", "LOG.md", "CURRENT.md"], 1):
            with self.subTest(stage=stage):
                request = self.request(number)
                with self.assertRaisesRegex(RuntimeError, "Injected interruption"):
                    self.publish(request, fail_after=stage)
                other = self.request(number + 10)
                before = self.state_files()
                self.expect_error(other, "RECOVERY_REQUIRED")
                self.assertEqual(before, self.state_files())
                self.assertEqual(self.publish(request)["status"], "COMMITTED")
                content = (self.home / "LOG.md").read_text()
                self.assertEqual(content.count(request["log_lines"][0]), 1)
                self.assertEqual(content.count("publication " + request["operation_id"]), 1)
                self.assertEqual(len([x for x in self.record_dir.iterdir() if x.suffix == ".md"]), 2 * number)

    def test_mid_record_interruption_retries_remaining_records(self):
        request = self.request()
        original = publisher.write
        def interrupt(path, raw, exclusive=False):
            if path.name == request["records"][1]["id"] + ".md":
                raise OSError("Synthetic interrupted record publication")
            return original(path, raw, exclusive)
        with mock.patch.object(publisher, "write", side_effect=interrupt):
            with self.assertRaises((OSError, registry.RegistryError)):
                self.publish(request)
        self.assertTrue((self.record_dir / (request["records"][0]["id"] + ".md")).exists())
        self.assertEqual(self.publish(request)["status"], "COMMITTED")
        self.assertEqual(len(list(self.record_dir.glob("*.md"))), 2)

    def test_changed_pending_record_preserves_user_bytes_and_refuses_recovery(self):
        request = self.request()
        with self.assertRaises(RuntimeError):
            self.publish(request, fail_after="records")
        changed = self.record_dir / (request["records"][0]["id"] + ".md")
        changed.write_text("Unexpected edit retained.\n")
        before = self.state_files()
        self.expect_error(request, "CONFLICT")
        self.assertEqual(before, self.state_files())

    def test_changed_log_after_preparation_preserves_edit_and_refuses_recovery(self):
        request = self.request()
        with self.assertRaises(RuntimeError):
            self.publish(request, fail_after="records")
        (self.home / "LOG.md").write_text("An unmanaged writer appended evidence.\n")
        before = self.state_files()
        self.expect_error(request, "CONFLICT")
        self.assertEqual(before, self.state_files())

    def test_changed_current_after_log_preserves_edit_and_refuses_recovery(self):
        request = self.request()
        with self.assertRaises(RuntimeError):
            self.publish(request, fail_after="LOG.md")
        changed = "Unmanaged CURRENT edit is not overwritten.\n"
        (self.home / "CURRENT.md").write_text(changed)
        before = self.state_files()
        self.expect_error(request, "CONFLICT")
        self.assertEqual(before, self.state_files())
        self.assertEqual((self.home / "CURRENT.md").read_text(), changed)

    def test_generation_change_waits_for_unfinished_recovery(self):
        request = self.request()
        with self.assertRaises(RuntimeError):
            self.publish(request, fail_after="journal")
        before_registry = self.registry.read_bytes()
        before = self.state_files()
        with self.assertRaises(registry.RegistryError) as caught:
            self.fixture.apply()
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        self.assertEqual(before_registry, self.registry.read_bytes())
        self.assertEqual(before, self.state_files())
        self.assertEqual(self.publish(request)["status"], "COMMITTED")
        self.assertEqual(self.fixture.apply()["generation"], self.generation + 1)

    def test_corrupt_pending_log_suffix_fails_before_log_or_current_write(self):
        old_log = "LOG-20261116-01 | Preserved history.\n"
        (self.home / "LOG.md").write_text(old_log)
        request = self.request()
        with self.assertRaises(RuntimeError):
            self.publish(request, fail_after="records")
        journal_path = self.journals / (request["operation_id"] + ".json")
        journal = json.loads(journal_path.read_text())
        journal["log_suffix"] += "Corrupted journal addition.\n"
        journal_path.write_text(json.dumps(journal))
        before = self.state_files()
        self.expect_error(request, "RECOVERY_REQUIRED")
        self.assertEqual(before, self.state_files())
        self.assertEqual((self.home / "LOG.md").read_text(), old_log)
        self.assertFalse((self.home / "CURRENT.md").exists())

    def test_committed_replay_detects_changed_record_without_repairing_it(self):
        request = self.request()
        self.publish(request)
        (self.record_dir / (request["records"][0]["id"] + ".md")).write_text("Unexpected edited record.\n")
        before = self.state_files()
        self.expect_error(request, "RECOVERY_REQUIRED")
        self.assertEqual(before, self.state_files())

    def test_staged_center_never_creates_publication_files(self):
        candidate = copy.deepcopy(self.fixture.data)
        candidate["projects"][0]["activation_state"] = "STAGED"
        center = json.loads((self.home / "project.json").read_text())
        center["activation_state"] = "STAGED"
        fixtures.write_json(self.home / "project.json", center)
        self.fixture.apply(candidate)
        before = self.state_files()
        self.expect_error(self.request(), "INACTIVE")
        self.assertEqual(before, self.state_files())

    def test_symlinked_publication_directory_never_creates_external_journal(self):
        target = self.base / "external-journals"; target.mkdir()
        self.journals.parent.mkdir()
        self.journals.symlink_to(target, target_is_directory=True)
        self.expect_error(self.request())
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((self.home / "LOG.md").exists())

    def test_symlinked_current_never_overwrites_external_target(self):
        target = self.base / "external.txt"; target.write_text("External value.\n")
        (self.home / "CURRENT.md").symlink_to(target)
        self.expect_error(self.request(expected_current_sha256=digest(target.read_bytes())))
        self.assertEqual(target.read_text(), "External value.\n")
        self.assertFalse((self.home / "LOG.md").exists())

    def test_symlinked_record_directory_never_writes_outside_home(self):
        target = self.base / "external"; target.mkdir()
        self.record_dir.symlink_to(target, target_is_directory=True)
        self.expect_error(self.request())
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((self.home / "LOG.md").exists())

    def test_hardlinked_record_never_mutates_other_name(self):
        target = self.base / "external.txt"; target.write_text("External value.\n")
        self.record_dir.mkdir()
        os.link(target, self.record_dir / "CON-20261117-01.md")
        self.expect_error(self.request())
        self.assertEqual(target.read_text(), "External value.\n")
        self.assertFalse((self.home / "LOG.md").exists())

    def test_fifo_current_fails_promptly_instead_of_blocking(self):
        request = self.request(current="Updated.\n")
        os.mkfifo(self.home / "CURRENT.md")
        args = self.process_args(request, "fifo-request")
        process = subprocess.run(args, capture_output=True, text=True, timeout=8)
        self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
        self.assertFalse((self.home / "LOG.md").exists())

    def test_log_overflow_is_rejected_before_journal_or_records(self):
        old = b"x" * 4000
        (self.home / "LOG.md").write_bytes(old)
        request = self.request()
        before = self.state_files()
        with mock.patch.object(publisher, "LIMIT", 4096):
            self.expect_error(request, "STORAGE_LIMIT")
        self.assertEqual(before, self.state_files())

    def test_journal_overflow_is_rejected_before_any_publication(self):
        request = self.request()
        request["records"][0]["text"] = "x" * 4000
        original = self.state_files()
        # Keep registry-readable inputs below this bound; serialized journal exceeds it.
        with mock.patch.object(registry, "MAX_JSON_BYTES", 4096):
            self.expect_error(request, "STORAGE_LIMIT")
        self.assertEqual(original, self.state_files())

    def test_duplicate_log_id_cannot_enter_history_in_another_operation(self):
        first = self.request()
        self.publish(first)
        second = self.request(number=2)
        second["log_lines"] = [first["log_lines"][0] + " Changed description."]
        before = self.state_files()
        self.expect_error(second)
        self.assertEqual(before, self.state_files())

    def test_abrupt_exit_after_exclusive_link_can_recover_journal_and_record(self):
        child = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
import publish_records as publisher
request = json.load(open(sys.argv[4]))
real_link = publisher.os.link

def link_then_die(*args, **kwargs):
    real_link(*args, **kwargs)
    if str(args[1]) == sys.argv[5]:
        os._exit(73)

publisher.os.link = link_then_die
publisher.publish(sys.argv[2], sys.argv[3], "codex", request)
"""
        for number, stage in enumerate(["journal", "record"], 1):
            with self.subTest(stage=stage):
                request = self.request(number)
                target_name = (request["operation_id"] + ".json" if stage == "journal"
                               else request["records"][0]["id"] + ".md")
                request_file = self.base / ("crash-" + stage + ".json")
                request_file.write_text(json.dumps(request))
                process = subprocess.run([sys.executable, "-c", child, str(ROOT / "scripts"),
                                          str(self.registry), str(self.repo), str(request_file), target_name],
                                         capture_output=True, text=True, timeout=10)
                self.assertEqual(process.returncode, 73, process.stdout + process.stderr)
                target = (self.journals if stage == "journal" else self.record_dir) / target_name
                self.assertEqual(target.stat().st_nlink, 2)
                self.assertEqual(self.publish(request)["status"], "COMMITTED")
                self.assertEqual(target.stat().st_nlink, 1)
                self.assertEqual(list(self.home.rglob(".publish-*")), [])
                self.assertEqual((self.home / "LOG.md").read_text().count(request["log_lines"][0]), 1)

    def test_two_competing_processes_commit_once_and_reject_stale_current(self):
        requests = [self.request(number=1), self.request(number=2)]
        processes = [subprocess.Popen(self.process_args(request, f"concurrent-{i}"), stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) for i, request in enumerate(requests)]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            results.append((process.returncode, stdout, stderr))
        self.assertEqual(sorted(x[0] for x in results), [0, 2], results)
        winner = next(i for i, x in enumerate(results) if x[0] == 0)
        loser = 1 - winner
        self.assertEqual(json.loads(results[loser][2])["status"], "CONFLICT")
        text = (self.home / "LOG.md").read_text()
        self.assertIn(requests[winner]["log_lines"][0], text)
        self.assertNotIn(requests[loser]["log_lines"][0], text)
        self.assertEqual(len(list(self.record_dir.glob("*.md"))), 2)

    def test_invalid_committed_journal_cannot_fabricate_successful_replay(self):
        request = self.request()
        self.journals.mkdir(parents=True)
        (self.journals / (request["operation_id"] + ".json")).write_text(json.dumps({
            "state": "COMMITTED", "request_sha256": digest(publisher.encode(request))}))
        before = self.state_files()
        self.expect_error(request)
        self.assertEqual(before, self.state_files())

    def test_malformed_existing_journal_returns_controlled_cli_error(self):
        self.journals.mkdir(parents=True)
        (self.journals / (str(uuid.uuid4()) + ".json")).write_text("{}\n")
        before = self.state_files()
        result = subprocess.run(self.process_args(self.request(), "malformed-journal"),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(before, self.state_files())


if __name__ == "__main__":
    unittest.main()
