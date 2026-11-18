#!/usr/bin/env python3
"""Offline checks for scripts/review_receipt.py and its call from scripts/outside_judge.py.

Run one class: python3 -B checks/test_review_receipt.py PureTests
Run all:       python3 -m unittest discover -s checks -p 'test_review_receipt.py' -v

PureTests builds values in memory. It patches builtins.open, os.open, os.mkdir, os.makedirs and
subprocess.run to raise, and it needs no temporary folder (it also passes with TMPDIR=/nonexistent).
StoreTests builds Git fixtures (a main worktree and a linked worktree) in a temporary folder and calls the
store functions. HelperTests and FaultTests run the helper as a process with fake provider commands, next to
the helper of commit bc63aae (fetched with `git show`), and compare the two. No model call runs.

The run stops at the first failure so that a mutation run is fast.
"""

import calendar
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
HELPER = SCRIPTS / "outside_judge.py"
BASELINE = "bc63aae"
sys.path.insert(0, str(SCRIPTS))
import review_capture  # noqa: E402
import review_receipt  # noqa: E402
import validate_handoff  # noqa: E402
import readback_settings  # noqa: E402

MAGIC = b"readback-manifest-v1\0"
REAL_GIT = shutil.which("git")


def sha(data):
    return hashlib.sha256(data).hexdigest()


C, H, R, F = (sha(word) for word in (b"contract", b"handoff", b"rules", b"helper file"))
MA, MB = sha(b"manifest a"), sha(b"manifest b")
KEY = sha(b"a review key")
ID1, ID2, ID3 = "1" * 32, "2" * 32, "3" * 32
ROUTE = {"provider": "claude", "model": "fable", "effort": "high", "source": "defaults"}
ROUTE_NON_ASCII = {"provider": "codex", "model": "modèle-é", "effort": "haut", "source": "table"}
VERSION = {"file_sha256": F}
REPO = {"root_name": "répo ✓", "worktree_root": "/work/répo ✓", "head": "f" * 40, "branch": "main"}
STATES = ("ok", "unsupported", "timeout", "error", "not-git")


def cap(state, manifest=None, reason=None):
    return {"state": state, "reason": reason, "manifest_sha256": manifest}


OK_A, OK_B = cap("ok", MA), cap("ok", MB)
VALID_OBJECT = {"valid": True, "findings": [], "validator_version": "1.1.0"}


def own_key(contract, handoff, rules, route, version, manifest):
    """The review key from the text of D8, written out here with json.dumps."""
    value = {"contract_sha256": contract, "handoff_sha256": handoff, "rules_sha256": rules, "route": route,
             "helper_version": version, "manifest_sha256": manifest}
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def own_phrase(validation):
    if validation["valid"] is True:
        return "validation true"
    if validation["valid"] is False:
        return "validation false %d" % len([f for f in validation["findings"] if f["level"] == "defect"])
    return "validation " + validation["valid"]


# ------------------------------------------------------------------- PureTests

class PureTests(unittest.TestCase):
    """In memory only. The file and process entry points raise during every test."""

    def setUp(self):
        def refuse(*args, **kwargs):
            raise AssertionError("a pure test touched the file system or started a process")
        for target in ("builtins.open", "os.open", "os.mkdir", "os.makedirs", "subprocess.run"):
            patcher = mock.patch(target, side_effect=refuse)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_guard_is_live(self):
        calls = (lambda: open(__file__), lambda: os.open(__file__, os.O_RDONLY), lambda: os.mkdir("guard-x"),
                 lambda: os.makedirs("guard-x"), lambda: subprocess.run(["true"]))
        for call in calls:
            with self.assertRaises(AssertionError):
                call()

    def test_review_key_matches_digests_computed_here(self):
        null_bytes = (
            '{"contract_sha256":"%s","handoff_sha256":"%s","helper_version":{"file_sha256":"%s"},'
            '"manifest_sha256":null,"route":{"effort":"haut","model":"modèle-é","provider":"codex",'
            '"source":"table"},"rules_sha256":"%s"}' % (C, H, F, R)).encode("utf-8")
        ok_bytes = null_bytes.replace(b'"manifest_sha256":null', b'"manifest_sha256":"' + MA.encode() + b'"')
        self.assertIn("è".encode("utf-8"), null_bytes)
        self.assertNotEqual(null_bytes, ok_bytes)
        key = review_receipt.review_key
        self.assertEqual(key(C, H, R, ROUTE_NON_ASCII, VERSION, OK_A), sha(ok_bytes))
        self.assertEqual(key(C, H, R, ROUTE_NON_ASCII, VERSION, OK_A),
                         own_key(C, H, R, ROUTE_NON_ASCII, VERSION, MA))
        for state in STATES[1:]:
            for manifest in (None, MA):
                with self.subTest(state=state, manifest=manifest):
                    self.assertEqual(key(C, H, R, ROUTE_NON_ASCII, VERSION, cap(state, manifest, "why")),
                                     sha(null_bytes))
        self.assertEqual(key(C, H, R, ROUTE, VERSION, OK_B), own_key(C, H, R, ROUTE, VERSION, MB))
        self.assertEqual(review_receipt.canonical_json({"b": "é", "a": [1, None]}), '{"a":[1,null],"b":"é"}'.encode())

    def test_every_input_changes_the_key(self):
        base = (C, H, R, ROUTE, VERSION, OK_A)
        reference = review_receipt.review_key(*base)
        self.assertEqual(reference, own_key(C, H, R, ROUTE, VERSION, MA))
        other = (sha(b"c2"), sha(b"h2"), sha(b"r2"), dict(ROUTE, source="table"), {"file_sha256": MB}, OK_B)
        for index, value in enumerate(other):
            changed = list(base)
            changed[index] = value
            with self.subTest(index=index):
                got = review_receipt.review_key(*changed)
                self.assertNotEqual(got, reference)
                manifest = changed[5]["manifest_sha256"]
                self.assertEqual(got, own_key(changed[0], changed[1], changed[2], changed[3], changed[4], manifest))
        for field in ("provider", "model", "effort"):
            with self.subTest(field=field):
                route = dict(ROUTE, **{field: "other"})
                self.assertNotEqual(review_receipt.review_key(C, H, R, route, VERSION, OK_A), reference)

    def test_binding_for_all_25_pairs(self):
        for first in STATES:
            for second in STATES:
                if first != "ok":
                    want = "unbound: before " + first
                elif second != "ok":
                    want = "unbound: after " + second
                else:
                    want = "ends-equal"
                a = cap(first, MA if first == "ok" else None, None if first == "ok" else "r1")
                b = cap(second, MA if second == "ok" else None, None if second == "ok" else "r2")
                with self.subTest(first=first, second=second):
                    self.assertEqual(review_receipt.binding(a, b), want)
        self.assertEqual(review_receipt.binding(cap("timeout", None, "x"), cap("error", None, "y")),
                         "unbound: before timeout")
        self.assertEqual(review_receipt.binding(cap("error", None, "x"), cap("timeout", None, "y")),
                         "unbound: before error")

    def test_binding_of_two_ok_captures_with_different_identities(self):
        self.assertEqual(review_receipt.binding(OK_A, OK_B), "unbound: changed")
        self.assertEqual(review_receipt.binding(OK_B, OK_A), "unbound: changed")
        self.assertEqual(review_receipt.binding(OK_A, dict(OK_A)), "ends-equal")

    def test_take_capture_keeps_three_fields_and_turns_exceptions_into_error(self):
        seen = []

        def full(repo, seconds):
            seen.append((repo, seconds))
            return {"manifest_version": 1, "state": "ok", "reason": None, "manifest_sha256": MA, "entries": 3,
                    "index_sha256": MB, "root": "/r"}
        self.assertEqual(review_receipt.take_capture(full, "/repo"), OK_A)
        self.assertEqual(seen, [("/repo", 5)])
        self.assertEqual(sorted(review_receipt.take_capture(full, "/repo")), ["manifest_sha256", "reason", "state"])
        bad = cap("error", None, "capture raised")
        bad["reason"] = "capture raised"
        for failure in (RuntimeError("x"), OSError("y"), BlockingIOError("z"), KeyError("k")):
            def raises(repo, seconds, failure=failure):
                raise failure
            with self.subTest(failure=failure):
                self.assertEqual(review_receipt.take_capture(raises, "/repo"), bad)
        self.assertEqual(review_receipt.take_capture(lambda repo, seconds: {}, "/repo"), bad)
        not_ok = {"state": "unsupported", "reason": "gitlink in the index", "manifest_sha256": None, "entries": None}
        self.assertEqual(review_receipt.take_capture(lambda repo, seconds: not_ok, "/repo"),
                         {"state": "unsupported", "reason": "gitlink in the index", "manifest_sha256": None})
        for stop in (KeyboardInterrupt, InterruptedError):
            def interrupted(repo, seconds, stop=stop):
                raise stop()
            with self.subTest(stop=stop):
                with self.assertRaises(stop):
                    review_receipt.take_capture(interrupted, "/repo")

    def started_expected(self, before, repository):
        return {
            "schema_version": 1, "attempt_id": ID1, "review_key": KEY, "created": "2026-11-17T10:20:30Z",
            "repository": repository, "capture_before": before,
            "contract": {"path": "/work/contract é.md", "sha256": C},
            "handoff": {"path": "/work/handoff.md", "sha256": H},
            "rules_sha256": R, "route": ROUTE_NON_ASCII, "helper_version": VERSION,
        }

    def test_started_record_has_exactly_these_fields_and_values(self):
        for before, repository in ((OK_A, REPO),
                                   (cap("unsupported", None, "gitlink in the index"),
                                    {"root_name": "r", "worktree_root": "/r", "head": None, "branch": None})):
            with self.subTest(before=before["state"]):
                got = review_receipt.started_record(
                    ID1, KEY, "2026-11-17T10:20:30Z", repository, before, "/work/contract é.md", C,
                    "/work/handoff.md", H, R, ROUTE_NON_ASCII, VERSION)
                self.assertEqual(got, self.started_expected(before, repository))
                self.assertEqual(sorted(got), sorted([
                    "schema_version", "attempt_id", "review_key", "created", "repository", "capture_before",
                    "contract", "handoff", "rules_sha256", "route", "helper_version"]))

    def test_completed_record_has_the_started_fields_and_exactly_seven_more(self):
        started = self.started_expected(OK_A, REPO)
        before = json.dumps(started, sort_keys=True)
        judge = {"verdict": "PASS", "provider": "claude", "requested_model": "fable", "effort": "high",
                 "reported_models": ["claude-fable-5-2"]}
        got = review_receipt.completed_record(started, OK_B, "unbound: changed", VALID_OBJECT, judge, sha(b"answer"), 0,
                                              [ID2, ID3])
        want = dict(started)
        want.update({"capture_after": OK_B, "binding": "unbound: changed", "validation": VALID_OBJECT,
                     "judge": judge, "answer_sha256": sha(b"answer"), "exit_status": 0, "duplicates_of": [ID2, ID3]})
        self.assertEqual(got, want)
        self.assertEqual(sorted(set(got) - set(started)), sorted([
            "capture_after", "binding", "validation", "judge", "answer_sha256", "exit_status", "duplicates_of"]))
        self.assertEqual(json.dumps(started, sort_keys=True), before)
        none = review_receipt.completed_record(started, cap("timeout", None, "t"), "unbound: after timeout",
                                               {"valid": "unavailable", "reason": "validator raised"},
                                               dict(judge, verdict="none", reported_models="unattested"), None, 3, [])
        self.assertIsNone(none["answer_sha256"])
        self.assertEqual((none["exit_status"], none["duplicates_of"], none["capture_after"]),
                         (3, [], cap("timeout", None, "t")))

    def test_judge_object_has_both_forms_of_reported_models(self):
        self.assertEqual(
            review_receipt.judge_object("PASS", "claude", "fable", "high", ["b-model", "a-model", "b-model"]),
            {"verdict": "PASS", "provider": "claude", "requested_model": "fable", "effort": "high",
             "reported_models": ["a-model", "b-model"]})
        self.assertEqual(
            review_receipt.judge_object("FAIL", "claude", "claude-fable-5-2", "low", {"claude-fable-5-2"}),
            {"verdict": "FAIL", "provider": "claude", "requested_model": "claude-fable-5-2", "effort": "low",
             "reported_models": ["claude-fable-5-2"]})
        self.assertEqual(
            review_receipt.judge_object("none", "codex", "gpt-6-astra", "high", None),
            {"verdict": "none", "provider": "codex", "requested_model": "gpt-6-astra", "effort": "high",
             "reported_models": "unattested"})
        self.assertEqual(review_receipt.judge_object("PASS", "claude", "f", "h", [])["reported_models"], [])

    def test_answer_digest_is_over_the_utf8_bytes_or_null(self):
        text = "PASS Évidence ✓\nline two\n"
        self.assertEqual(review_receipt.answer_sha256(text), sha(text.encode("utf-8")))
        self.assertEqual(review_receipt.answer_sha256(""), sha(b""))
        self.assertIsNone(review_receipt.answer_sha256(None))

    def test_validation_object_for_each_result_of_the_validator(self):
        contract = ("CON-20261117-01  Do the thing.\nACC-1  The tool shall work.   Check: sh a.sh\n"
                    "Out of scope: everything else\n")
        handoff = ("id        HND-20261117-01\nfrom      worker\ndate      2026-11-17\nstate     DONE\n"
                   "readback  task: x | limits: y | done when: ACC-1\nsummary   Done.\nchanges   none\ncommit    none\n"
                   "checks    sh a.sh | exit 0 | ok\ncontract  ACC-1 VERIFIED\nopen      -\nissues    -\nnext      Accept.\n")
        true = review_receipt.run_validation(validate_handoff.validate, contract, handoff)
        self.assertIs(true["valid"], True)
        self.assertEqual(review_receipt.validation_phrase(true), "validation true")
        false = review_receipt.run_validation(validate_handoff.validate, contract, "id HND\n")
        self.assertIs(false["valid"], False)
        defects = len([f for f in false["findings"] if f["level"] == "defect"])
        self.assertGreater(defects, 2)
        self.assertEqual(review_receipt.validation_phrase(false), "validation false %d" % defects)
        unparsed = review_receipt.run_validation(validate_handoff.validate, contract, "{not json")
        self.assertEqual(unparsed["valid"], "unparsed")
        self.assertEqual(review_receipt.validation_phrase(unparsed), "validation unparsed")
        calls = []

        def validate(contract_text, handoff_text):
            calls.append((contract_text, handoff_text))
            return {"valid": False, "findings": [{"level": "defect"}], "validator_version": "x"}
        self.assertEqual(review_receipt.run_validation(validate, "c", "h"),
                         {"valid": False, "findings": [{"level": "defect"}], "validator_version": "x"})
        self.assertEqual(calls, [("c", "h")])

    def test_validation_is_unavailable_without_a_validator_or_when_it_raises(self):
        self.assertEqual(review_receipt.run_validation(None, "c", "h"),
                         {"valid": "unavailable", "reason": "validator not importable"})

        def raises(contract_text, handoff_text):
            raise ValueError("bad")
        got = review_receipt.run_validation(raises, "c", "h")
        self.assertEqual(got, {"valid": "unavailable", "reason": "validator raised"})
        self.assertEqual(review_receipt.validation_phrase(got), "validation unavailable")
        for stop in (KeyboardInterrupt, InterruptedError):
            def interrupted(contract_text, handoff_text, stop=stop):
                raise stop()
            with self.subTest(stop=stop):
                with self.assertRaises(stop):
                    review_receipt.run_validation(interrupted, "c", "h")

    def test_validation_phrase_counts_only_defects(self):
        mixed = [{"level": "defect"}, {"level": "unresolved"}, {"level": "defect"}, {"level": "unresolved"},
                 {"level": "unresolved"}]
        self.assertEqual(review_receipt.validation_phrase({"valid": False, "findings": mixed}), "validation false 2")
        self.assertEqual(review_receipt.validation_phrase({"valid": False, "findings": mixed[:1]}), "validation false 1")
        self.assertEqual(review_receipt.validation_phrase({"valid": "unparsed", "findings": mixed[1:2]}),
                         "validation unparsed")
        self.assertEqual(review_receipt.validation_phrase({"valid": True, "findings": []}), "validation true")
        self.assertEqual(review_receipt.validation_phrase({"valid": "unavailable", "reason": "r"}),
                         "validation unavailable")

    def test_both_forms_of_the_receipt_line(self):
        false = {"valid": False, "findings": [{"level": "defect"}, {"level": "unresolved"}]}
        self.assertEqual(
            review_receipt.receipt_line(ID1, "unbound: before error", false, "/p/ath.completed.json"),
            "receipt    " + ID1 + " | unbound: before error | validation false 1 | /p/ath.completed.json")
        self.assertEqual(
            review_receipt.receipt_line(ID2, "ends-equal", VALID_OBJECT, "/tmp/répo/x.completed.json"),
            "receipt    " + ID2 + " | ends-equal | validation true | /tmp/répo/x.completed.json")
        self.assertEqual(
            review_receipt.receipt_line(ID3, "unbound: changed", {"valid": "unparsed", "findings": []}, "/p"),
            "receipt    " + ID3 + " | unbound: changed | validation unparsed | /p")
        self.assertEqual(
            review_receipt.receipt_line(ID3, "ends-equal", {"valid": "unavailable", "reason": "r"}, "/p"),
            "receipt    " + ID3 + " | ends-equal | validation unavailable | /p")
        self.assertEqual(review_receipt.none_line("no provider call"), "receipt    none | no provider call")
        self.assertEqual(review_receipt.none_line("cannot write the record"),
                         "receipt    none | cannot write the record")

    def test_line_breaks_and_bars_are_replaced_in_every_field(self):
        got = review_receipt.receipt_line("id\n1|x", "b\rb|b", VALID_OBJECT, "/tmp/a|b\nc\rd é|")
        self.assertEqual(got, "receipt    id 1_x | b b_b | validation true | /tmp/a_b c d é_")
        self.assertEqual(review_receipt.none_line("why\nnot|really\r."), "receipt    none | why not_really .")
        self.assertNotIn("\n", got)
        self.assertEqual(got.count("|"), 3)
        self.assertEqual(review_receipt.none_line("x").count("|"), 1)


# ------------------------------------------------------------------ test tools

def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


GIT_IDENTITY = {
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def git(repo, *args):
    env = clean_env()
    env.update(GIT_IDENTITY)
    result = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


def make_repo(parent, name="repo"):
    """A repository with one commit. The commit id is the same for every call with the same name."""
    repo = Path(parent) / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("alpha\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.txt").write_text("beta\n")
    (repo / "run.sh").write_text("#!/bin/sh\n")
    (repo / "run.sh").chmod(0o755)
    os.symlink("a.txt", repo / "link")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def own_identity(root):
    """The manifest identity of a worktree from the documented byte format and own reads."""
    root = str(root)
    cached = git_bytes(root, "ls-files", "-z", "--cached")
    others = git_bytes(root, "ls-files", "-z", "--others", "--exclude-standard")
    paths = sorted(set(p for p in cached.split(b"\0") + others.split(b"\0") if p))
    blob = MAGIC
    for path in paths:
        full = os.path.join(os.fsencode(root), path)
        info = os.lstat(full)
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(full)
            record = b"l - " + sha(target).encode() + b" " + str(len(target)).encode()
        else:
            data = Path(os.fsdecode(full)).read_bytes()
            record = (b"f x " if info.st_mode & 0o100 else b"f - ") + sha(data).encode() + b" " + str(len(data)).encode()
        blob += record + b"\0" + path + b"\0"
    return sha(blob)


def git_bytes(root, *args):
    env = clean_env()
    env.update(GIT_IDENTITY)
    result = subprocess.run(["git", "--no-optional-locks", "-C", root, *args], env=env, capture_output=True,
                            timeout=30)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


def file_map(folder):
    """Every file below folder as {relative path: sha256 of its bytes}."""
    found = {}
    for dirpath, _names, files in os.walk(folder):
        for name in files:
            path = os.path.join(dirpath, name)
            found[os.path.relpath(path, folder)] = "link" if os.path.islink(path) else sha(Path(path).read_bytes())
    return found


def dir_list(folder):
    found = []
    for dirpath, names, _files in os.walk(folder):
        found.extend(os.path.relpath(os.path.join(dirpath, name), folder) for name in names)
    return sorted(found)


def modes(folder):
    """{relative path: permission bits} for every entry below folder."""
    found = {}
    for dirpath, names, files in os.walk(folder):
        for name in names + files:
            path = os.path.join(dirpath, name)
            found[os.path.relpath(path, folder)] = stat.S_IMODE(os.lstat(path).st_mode)
    return found


# ------------------------------------------------------------------ StoreTests

class StoreTests(unittest.TestCase):
    """A main worktree and a linked worktree in a temporary folder; the records go below the main .git."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="readback-store-")
        self.addCleanup(temp.cleanup)
        self.base = Path(os.path.realpath(temp.name))
        self.main = make_repo(self.base, "main repo")
        self.linked = self.base / "linked tree"
        git(self.main, "worktree", "add", "-q", "-b", "side", str(self.linked))
        self.git_dir = os.path.realpath(self.main / ".git")

    def fixtures(self):
        """Yield (name, worktree, common directory) with no readback folder at the start of each."""
        for name, repo in (("main", self.main), ("linked", self.linked)):
            common = review_capture.git_common_dir(repo)
            self.assertEqual(common, self.git_dir)
            shutil.rmtree(os.path.join(common, "readback"), ignore_errors=True)
            yield name, repo, common

    def worktree_files(self, repo):
        return {k: v for k, v in file_map(repo).items() if not k.startswith(".git/")}

    def started(self, attempt=ID1, key=KEY):
        return review_receipt.started_record(attempt, key, "2026-11-17T10:20:30Z", REPO, OK_A, "/c.md", C,
                                             "/h.md", H, R, ROUTE, VERSION)

    def completed(self, attempt=ID1, key=KEY, after=OK_A):
        judge = review_receipt.judge_object("PASS", "claude", "fable", "high", ["claude-fable-5-2"])
        return review_receipt.completed_record(
            self.started(attempt, key), after, review_receipt.binding(OK_A, after), VALID_OBJECT, judge,
            sha(b"answer"), 0, [])

    def test_paths_below_the_common_directory_of_the_main_repository(self):
        for name, repo, common in self.fixtures():
            worktree_before = self.worktree_files(repo)
            started = self.started()
            completed = self.completed()
            path = review_receipt.store_started(common, started)
            self.assertEqual(path, os.path.join(self.git_dir, "readback", "reviews", KEY, ID1 + ".started.json"))
            done, marker = review_receipt.store_completed(common, completed)
            self.assertEqual(done, os.path.join(self.git_dir, "readback", "reviews", KEY, ID1 + ".completed.json"))
            self.assertEqual(marker, os.path.join(self.git_dir, "readback", "by-manifest", MA, ID1))
            self.assertEqual(json.loads(Path(path).read_text()), started)
            self.assertEqual(json.loads(Path(done).read_text()), completed)
            self.assertEqual(Path(marker).read_bytes(), b"")
            readback = os.path.join(self.git_dir, "readback")
            self.assertEqual(sorted(file_map(readback)), [
                "by-manifest/" + MA + "/" + ID1, "reviews/" + KEY + "/" + ID1 + ".completed.json",
                "reviews/" + KEY + "/" + ID1 + ".started.json"])
            self.assertEqual(dir_list(readback), ["by-manifest", "by-manifest/" + MA, "reviews", "reviews/" + KEY])
            self.assertEqual(self.worktree_files(repo), worktree_before)
            self.assertFalse(any("readback" in p for p in file_map(os.path.join(self.git_dir, "worktrees"))))

    def test_modes_of_created_folders_and_files_and_unchanged_modes_of_existing_ones(self):
        for name, repo, common in self.fixtures():
            for mask in (0o022, 0o077, 0o000):
                with self.subTest(mask=oct(mask)):
                    shutil.rmtree(os.path.join(common, "readback"), ignore_errors=True)
                    os.chmod(common, 0o750)
                    os.chmod(os.path.join(common, "objects"), 0o751)
                    os.chmod(os.path.join(common, "refs"), 0o705)
                    before = {k: v for k, v in modes(common).items() if not k.startswith("readback")}
                    old = os.umask(mask)
                    try:
                        review_receipt.store_started(common, self.started())
                        review_receipt.store_completed(common, self.completed())
                    finally:
                        os.umask(old)
                    after = modes(common)
                    self.assertEqual({k: v for k, v in after.items() if not k.startswith("readback")}, before)
                    self.assertEqual(stat.S_IMODE(os.stat(common).st_mode), 0o750)
                    created = {k: v for k, v in after.items() if k.startswith("readback")}
                    self.assertEqual(sorted(created), sorted([
                        "readback", "readback/reviews", "readback/reviews/" + KEY,
                        "readback/reviews/" + KEY + "/" + ID1 + ".started.json",
                        "readback/reviews/" + KEY + "/" + ID1 + ".completed.json",
                        "readback/by-manifest", "readback/by-manifest/" + MA, "readback/by-manifest/" + MA + "/" + ID1]))
                    for path, mode in created.items():
                        wanted = 0o700 if os.path.isdir(os.path.join(common, path)) else 0o600
                        self.assertEqual(mode, wanted, (path, oct(mode)))

    def test_a_folder_that_exists_keeps_its_mode(self):
        for name, repo, common in self.fixtures():
            os.mkdir(os.path.join(common, "readback"))
            os.chmod(os.path.join(common, "readback"), 0o755)
            os.mkdir(os.path.join(common, "readback", "reviews"))
            os.chmod(os.path.join(common, "readback", "reviews"), 0o750)
            review_receipt.store_started(common, self.started())
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(common, "readback")).st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(common, "readback", "reviews")).st_mode), 0o750)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(common, "readback", "reviews", KEY)).st_mode), 0o700)

    def test_marker_only_after_a_stored_completed_record_with_an_ok_second_capture(self):
        for name, repo, common in self.fixtures():
            review_receipt.store_started(common, self.started())
            self.assertFalse(os.path.exists(os.path.join(common, "readback", "by-manifest")))
            path, marker = review_receipt.store_completed(common, self.completed())
            self.assertEqual(marker, os.path.join(common, "readback", "by-manifest", MA, ID1))
            self.assertTrue(os.path.isfile(marker))
        for state in STATES[1:]:
            for name, repo, common in self.fixtures():
                with self.subTest(state=state):
                    after = cap(state, None, "why")
                    review_receipt.store_started(common, self.started())
                    path, marker = review_receipt.store_completed(common, self.completed(after=after))
                    self.assertIsNone(marker)
                    self.assertEqual(path, os.path.join(common, "readback", "reviews", KEY, ID1 + ".completed.json"))
                    self.assertFalse(os.path.exists(os.path.join(common, "readback", "by-manifest")))
        for name, repo, common in self.fixtures():
            review_receipt.store_started(common, self.started())
            review_receipt.store_completed(common, self.completed(after=OK_B))
            self.assertEqual(sorted(os.listdir(os.path.join(common, "readback", "by-manifest"))), [MB])
        for name, repo, common in self.fixtures():
            review_receipt.store_started(common, self.started())
            review_receipt.store_completed(common, self.completed())
            with self.assertRaises(FileExistsError):
                review_receipt.store_completed(common, self.completed(after=OK_B))
            self.assertEqual(os.listdir(os.path.join(common, "readback", "by-manifest")), [MA])

    def test_refusal_to_replace_a_started_file_a_completed_file_and_a_marker(self):
        for name, repo, common in self.fixtures():
            review_receipt.store_started(common, self.started())
            done, marker = review_receipt.store_completed(common, self.completed())
            started_path = os.path.join(os.path.dirname(done), ID1 + ".started.json")
            before = file_map(os.path.join(common, "readback"))
            with self.assertRaises(FileExistsError):
                review_receipt.store_started(common, dict(self.started(), created="2026-11-18T00:00:00Z"))
            with self.assertRaises(FileExistsError):
                review_receipt.store_completed(common, dict(self.completed(), exit_status=3))
            self.assertEqual(file_map(os.path.join(common, "readback")), before)
            self.assertEqual(json.loads(Path(started_path).read_text())["created"], "2026-11-17T10:20:30Z")
            self.assertEqual(json.loads(Path(done).read_text())["exit_status"], 0)
        for name, repo, common in self.fixtures():
            marker_folder = os.path.join(common, "readback", "by-manifest", MA)
            os.makedirs(marker_folder)
            Path(marker_folder, ID1).write_bytes(b"keep this marker")
            review_receipt.store_started(common, self.started())
            done, marker = review_receipt.store_completed(common, self.completed())
            self.assertIsNone(marker)
            self.assertEqual(Path(marker_folder, ID1).read_bytes(), b"keep this marker")
            self.assertEqual(sorted(os.listdir(marker_folder)), [ID1])
            self.assertTrue(os.path.isfile(done))

    def test_no_temporary_file_after_a_success_and_after_a_failed_write(self):
        for name, repo, common in self.fixtures():
            review_receipt.store_started(common, self.started())
            review_receipt.store_completed(common, self.completed())
            self.assertEqual(sorted(file_map(os.path.join(common, "readback"))), [
                "by-manifest/" + MA + "/" + ID1, "reviews/" + KEY + "/" + ID1 + ".completed.json",
                "reviews/" + KEY + "/" + ID1 + ".started.json"])

        def part_way(fd, data):
            os.write(fd, data[:len(data) // 2])
            os.close(fd)
            raise OSError(errno.ENOSPC, "no space")

        def sync_fails(fd):
            raise OSError(errno.EIO, "no sync")

        def link_fails(source, target):
            raise OSError(errno.EXDEV, "no link")
        for label, patcher in (("part way", mock.patch.object(review_receipt, "_write", part_way)),
                               ("sync", mock.patch.object(os, "fsync", sync_fails)),
                               ("link", mock.patch.object(os, "link", link_fails))):
            for name, repo, common in self.fixtures():
                with self.subTest(failure=label):
                    with patcher:
                        with self.assertRaises(OSError):
                            review_receipt.store_started(common, self.started())
                    self.assertEqual(file_map(os.path.join(common, "readback")), {})
                    self.assertEqual(os.listdir(os.path.join(common, "readback", "reviews", KEY)), [])
        for name, repo, common in self.fixtures():
            review_receipt.store_started(common, self.started())
            with mock.patch.object(review_receipt, "_write", part_way):
                with self.assertRaises(OSError):
                    review_receipt.store_completed(common, self.completed())
            self.assertEqual(sorted(file_map(os.path.join(common, "readback"))),
                             ["reviews/" + KEY + "/" + ID1 + ".started.json"])
        for name, repo, common in self.fixtures():
            review_receipt.store_started(common, self.started())
            calls = []

            def second_write_fails(fd, data, real=review_receipt._write):
                calls.append(len(data))
                if len(calls) == 2:
                    return part_way(fd, data)
                return real(fd, data)
            with mock.patch.object(review_receipt, "_write", second_write_fails):
                path, marker = review_receipt.store_completed(common, self.completed())
            self.assertIsNone(marker)
            self.assertEqual(calls[1], 0)
            self.assertEqual(sorted(file_map(os.path.join(common, "readback"))), [
                "reviews/" + KEY + "/" + ID1 + ".completed.json", "reviews/" + KEY + "/" + ID1 + ".started.json"])

    def test_a_record_is_written_synced_linked_and_unlinked_in_this_order(self):
        for name, repo, common in self.fixtures():
            events = []
            real = {"open": os.open, "fsync": os.fsync, "link": os.link, "unlink": os.unlink}

            def spy(label):
                def call(*args):
                    events.append((label,) + args)
                    return real[label](*args)
                return call
            with mock.patch.object(os, "open", spy("open")), mock.patch.object(os, "fsync", spy("fsync")), \
                    mock.patch.object(os, "link", spy("link")), mock.patch.object(os, "unlink", spy("unlink")):
                final = review_receipt.store_started(common, self.started())
            self.assertEqual([e[0] for e in events], ["open", "fsync", "link", "unlink"])
            opened, synced, linked, unlinked = events
            temporary = opened[1]
            self.assertEqual(os.path.dirname(temporary), os.path.dirname(final))
            self.assertNotEqual(temporary, final)
            self.assertEqual(opened[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            self.assertEqual(opened[3], 0o600)
            self.assertIsInstance(synced[1], int)
            self.assertEqual(linked[1:], (temporary, final))
            self.assertEqual(unlinked[1:], (temporary,))
            self.assertEqual(os.listdir(os.path.dirname(final)), [os.path.basename(final)])

    def test_unwritable_location_stores_nothing(self):
        for name, repo, common in self.fixtures():
            Path(common, "readback").write_bytes(b"a file, not a folder")
            before = file_map(common)
            with self.assertRaises(OSError):
                review_receipt.store_started(common, self.started())
            with self.assertRaises(OSError):
                review_receipt.store_completed(common, self.completed())
            self.assertEqual(file_map(common), before)

    def test_duplicates_of_lists_valid_completed_records_of_the_key_at_one_scan(self):
        for name, repo, common in self.fixtures():
            self.assertEqual(review_receipt.duplicates_of(common, KEY, ID1), [])
            folder = os.path.join(common, "readback", "reviews", KEY)
            for attempt in (ID3, ID1, ID2):
                review_receipt.store_started(common, self.started(attempt))
            review_receipt.store_completed(common, self.completed(ID3))
            review_receipt.store_completed(common, self.completed(ID1))
            self.assertEqual(review_receipt.duplicates_of(common, KEY, ID2), [ID1, ID3])
            self.assertEqual(review_receipt.duplicates_of(common, KEY, ID1), [ID3])
            self.assertEqual(review_receipt.duplicates_of(common, KEY, ID3), [ID1])
            other = sha(b"another key")
            review_receipt.store_started(common, self.started(ID2, other))
            review_receipt.store_completed(common, self.completed(ID2, other))
            self.assertEqual(review_receipt.duplicates_of(common, KEY, ID2), [ID1, ID3])
            self.assertEqual(review_receipt.duplicates_of(common, other, ID1), [ID2])
            self.assertEqual(review_receipt.duplicates_of(common, other, ID2), [])
            for bad, content in (("4" * 32, b"{not json"), ("5" * 32, b'{"schema_version": 2, "attempt_id": "' + b"5" * 32 + b'"}'),
                                 ("6" * 32, b'{"schema_version": 1, "attempt_id": "' + b"7" * 32 + b'"}'),
                                 ("8" * 32, b'[1, 2]'), ("9" * 32, b'{"schema_version": true, "attempt_id": "' + b"9" * 32 + b'"}')):
                Path(folder, bad + ".completed.json").write_bytes(content)
            Path(folder, "a" * 32 + ".started.json").write_bytes(
                b'{"schema_version": 1, "attempt_id": "' + b"a" * 32 + b'"}')
            self.assertEqual(review_receipt.duplicates_of(common, KEY, ID2), [ID1, ID3])

    def partial_folder(self, common):
        folder = os.path.join(common, "readback", "reviews", KEY)
        os.makedirs(folder)
        return folder

    def test_listing_of_a_started_only_attempt_and_of_partial_files(self):
        for name, repo, common in self.fixtures():
            self.assertEqual(review_receipt.list_unfinished(common), {"indeterminate": [], "partial": []})
            review_receipt.store_started(common, self.started(ID1))
            self.assertEqual(review_receipt.list_unfinished(common),
                             {"indeterminate": [(KEY, ID1)], "partial": []})
            review_receipt.store_started(common, self.started(ID2))
            review_receipt.store_completed(common, self.completed(ID2))
            review_receipt.store_started(common, self.started(ID3))
            other = sha(b"another key")
            review_receipt.store_started(common, self.started(ID1, other))
            self.assertEqual(review_receipt.list_unfinished(common), {
                "indeterminate": sorted([(KEY, ID1), (KEY, ID3), (other, ID1)]), "partial": []})
            review_receipt.store_completed(common, self.completed(ID1, other))
            self.assertEqual(review_receipt.list_unfinished(common)["indeterminate"], [(KEY, ID1), (KEY, ID3)])

    def test_listing_names_each_kind_of_partial_file(self):
        for name, repo, common in self.fixtures():
            folder = self.partial_folder(common)
            review_receipt.store_started(common, self.started(ID1))
            good = Path(folder, ID1 + ".started.json").read_bytes()
            wrong = {
                "notes.txt": b"hello",
                ID2 + ".started.json.bak": good,
                ".tmp-0123456789abcdef": b"half a record",
                ID2 + ".started.json": b"{not json",
                ID3 + ".started.json": b'{"schema_version": 2, "attempt_id": "' + ID3.encode() + b'"}',
                "4" * 32 + ".completed.json": b'{"schema_version": 1, "attempt_id": "' + ID2.encode() + b'"}',
                "5" * 32 + ".completed.json": b'{"schema_version": true, "attempt_id": "' + b"5" * 32 + b'"}',
                "6" * 32 + ".completed.json": b'{"schema_version": 1.0, "attempt_id": "' + b"6" * 32 + b'"}',
                "7" * 32 + ".completed.json": b'["schema_version", 1]',
                "8" * 32 + ".completed.json": b'{"attempt_id": "' + b"8" * 32 + b'"}',
                "9" * 32 + ".started.json": b'\xff\xfe not utf-8',
                "a" * 32 + ".started.json": b"[" * 100000,
                "b" * 32 + ".STARTED.JSON": good,
                "c" * 32 + ".started.json.started.json": b'{"schema_version": 1, "attempt_id": "' + b"c" * 32 + b'"}',
            }
            for file_name, content in wrong.items():
                Path(folder, file_name).write_bytes(content)
            os.mkdir(os.path.join(folder, "d" * 32 + ".started.json"))
            os.symlink(ID1 + ".started.json", os.path.join(folder, "e" * 32 + ".started.json"))
            Path(os.path.join(common, "readback", "reviews"), "stray.txt").write_bytes(b"x")
            listing = review_receipt.list_unfinished(common)
            names = list(wrong) + ["d" * 32 + ".started.json", "e" * 32 + ".started.json"]
            reviews = os.path.join(common, "readback", "reviews")
            self.assertLess(KEY, "stray.txt")
            self.assertEqual(listing["partial"],
                             [os.path.join(folder, n) for n in sorted(names)] + [os.path.join(reviews, "stray.txt")])
            self.assertEqual(listing["indeterminate"], [(KEY, ID1)])
            self.assertEqual(review_receipt.duplicates_of(common, KEY, ID2), [])

    def test_listing_does_not_open_a_named_pipe(self):
        for name, repo, common in self.fixtures():
            folder = self.partial_folder(common)
            os.mkfifo(os.path.join(folder, ID1 + ".started.json"))

            def too_slow(signum, frame):
                raise TimeoutError("the listing waited on a pipe")
            old = signal.signal(signal.SIGALRM, too_slow)
            signal.alarm(5)
            try:
                listing = review_receipt.list_unfinished(common)
                dups = review_receipt.duplicates_of(common, KEY, ID2)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old)
            self.assertEqual(listing, {"indeterminate": [], "partial": [os.path.join(folder, ID1 + ".started.json")]})
            self.assertEqual(dups, [])

    def test_repository_info_reads_root_head_and_branch_once(self):
        for name, repo, common in self.fixtures():
            head = git(repo, "rev-parse", "HEAD").strip()
            self.assertEqual(len(head), 40)
            want = {"root_name": repo.name, "worktree_root": os.path.realpath(repo), "head": head,
                    "branch": "main" if name == "main" else "side"}
            self.assertEqual(review_receipt.repository_info(repo / "sub"), want)
        git(self.linked, "checkout", "-q", "--detach")
        self.assertEqual(review_receipt.repository_info(self.linked)["branch"], None)
        self.assertEqual(review_receipt.repository_info(self.linked)["head"], git(self.linked, "rev-parse", "HEAD").strip())
        empty = self.base / "empty repo"
        empty.mkdir()
        git(empty, "init", "-q", "-b", "trunk")
        self.assertEqual(review_receipt.repository_info(empty), {
            "root_name": "empty repo", "worktree_root": os.path.realpath(empty), "head": None, "branch": "trunk"})
        plain = self.base / "plain folder"
        plain.mkdir()
        self.assertIsNone(review_receipt.repository_info(plain))
        self.assertIsNone(review_receipt.repository_info(self.base / "missing folder"))
        self.assertIsNone(review_receipt.repository_info(self.main / ".git"))
        with mock.patch.dict(os.environ, {"GIT_DIR": str(empty / ".git")}):
            self.assertEqual(review_receipt.repository_info(self.main)["branch"], "main")

    def test_helper_version_and_clock(self):
        path = self.base / "helper copy.py"
        path.write_bytes(b"print('x')\r\n\xc3\xa9")
        self.assertEqual(review_receipt.helper_version(str(path)), {"file_sha256": sha(b"print('x')\r\n\xc3\xa9")})
        before = time.time()
        stamp = review_receipt.now_utc()
        after = time.time()
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        seconds = calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
        self.assertLessEqual(int(before) - 1, seconds)
        self.assertLessEqual(seconds, after + 1)


# ------------------------------------------------------------- helper process

FAKE_CLI = r'''
import json, os, pathlib, signal, sys, time
provider = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
prompt = sys.stdin.buffer.read()
with open(os.environ["FAKE_RECORD"], "a") as record:
    record.write(json.dumps({
        "provider": provider, "args": args, "prompt_hex": prompt.hex(), "cwd": os.getcwd(),
        "effort_env": os.environ.get("CLAUDE_CODE_EFFORT_LEVEL"),
        "judge_active": os.environ.get("READBACK_JUDGE_ACTIVE"),
    }) + "\n")
if provider == "codex":
    if not (args[:1] == ["exec"] and args[-1:] == ["-"] and "-o" in args):
        sys.exit(96)
elif "--output-format" not in args or "--model" not in args:
    sys.exit(96)
if not prompt.strip() or os.environ.get("READBACK_JUDGE_ACTIVE") != "1":
    sys.exit(96)
mode = os.environ.get("FAKE_MODE", "success")
if mode == "auth_error":
    print("PASS this must not override the exit code")
    sys.exit(17)
if mode == "sleep":
    time.sleep(60)
if mode == "interrupt_parent":
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(60)
answer = os.environ["FAKE_ANSWER"]
if provider == "codex":
    pathlib.Path(args[args.index("-o") + 1]).write_bytes(answer.encode("utf-8"))
else:
    envelope = {"type": "result", "subtype": "success", "is_error": False, "result": answer,
                "modelUsage": json.loads(os.environ["FAKE_USAGE"])}
    if os.environ.get("FAKE_MODEL"):
        envelope["model"] = os.environ["FAKE_MODEL"]
    print(json.dumps(envelope))
'''

PASS_ANSWER = "PASS Evidence supports the handoff.\nKeep this complete finding.\n"
FAIL_ANSWER = "FAIL ACC-2 has no evidence.\nThe failed command returned exit 1.\n"
CONTRACT_TEXT = ("CON-20261117-01  Do the thing.\nACC-1  The tool shall work.   Check: sh a.sh\n"
                 "Out of scope: everything else\n")
HANDOFF_TEXT = ("id        HND-20261117-01\nfrom      worker\ndate      2026-11-17\nstate     DONE\n"
                "readback  task: x | limits: y | done when: ACC-1\nsummary   Done.\nchanges   none\ncommit    none\n"
                "checks    sh a.sh | exit 0 | ok\ncontract  ACC-1 VERIFIED\nopen      -\nissues    -\nnext      Accept.\n")
HOSTS = (("claude", "codex"), ("codex", "claude"))  # (host, provider)
_oracle_cache = {}


def oracle_sources():
    """The helper and its rules as they were at bc63aae."""
    if not _oracle_cache:
        for name, path in (("helper", "scripts/outside_judge.py"), ("rules", "agents/judge.md")):
            result = subprocess.run(["git", "-C", str(ROOT), "show", BASELINE + ":" + path], env=clean_env(),
                                    capture_output=True, check=True, timeout=30)
            _oracle_cache[name] = result.stdout
    return _oracle_cache


def new_world(base):
    """A folder with fake providers, the real git behind a symlink, the oracle helper and an environment."""
    base = Path(base)
    (base / "bin").mkdir()
    (base / "gitbin").mkdir()
    (base / "empty").mkdir()
    (base / "tmp").mkdir()
    (base / "home").mkdir()
    (base / "gitbin" / "git").symlink_to(REAL_GIT)
    for provider in ("claude", "codex"):
        fake = base / "bin" / provider
        fake.write_text("#!" + sys.executable + "\n" + FAKE_CLI)
        fake.chmod(0o755)
    (base / "oracle").mkdir()
    sources = oracle_sources()
    (base / "oracle" / "outside_judge.py").write_bytes(sources["helper"])
    (base / "oracle" / "judge.md").write_bytes(sources["rules"])
    env = {
        "HOME": os.path.realpath(base / "home"),  # no session variable is set: the project mode decides
        "PATH": str(base / "bin") + os.pathsep + str(base / "gitbin"), "TMPDIR": str(base / "tmp"),
        "READBACK_TEAMMATES": str(base / "TEAMMATES.md"), "READBACK_JUDGE_SECONDS": "30",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8",
        "FAKE_ANSWER": PASS_ANSWER, "FAKE_USAGE": json.dumps({"claude-fable-5-2": {"inputTokens": 10}}),
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }
    return types.SimpleNamespace(base=base, env=env, counter=[0])


def fresh_dir(world, label):
    world.counter[0] += 1
    folder = world.base / ("%s-%d" % (label, world.counter[0]))
    folder.mkdir()
    return folder


def default_sigint():
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # a helper started from a background shell would ignore SIGINT


def run_helper(world, helper, host, contract, handoff, repo, extra_env=None, command=None, interruptible=False):
    """Run a helper as a process. Return its status, output, error and the provider calls it made.

    The record of the fake providers is one file for every run. It is read and deleted after each run.
    """
    record = world.base / "calls.jsonl"
    assert not record.exists()
    env = dict(world.env)
    env["FAKE_RECORD"] = str(record)
    env.update(extra_env or {})
    argv = command or [sys.executable, "-B", str(helper)]
    result = subprocess.run([*argv, "--host", host, str(contract), str(handoff), str(repo)], env=env,
                            capture_output=True, timeout=90, preexec_fn=default_sigint if interruptible else None)
    calls = [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
    for call in calls:  # the prompt is recorded as hex, so that no byte is lost
        call["prompt"] = bytes.fromhex(call.pop("prompt_hex"))
    if record.exists():
        record.unlink()
    return types.SimpleNamespace(code=result.returncode, out=result.stdout, err=result.stderr, calls=calls,
                                 text=result.stdout.decode("utf-8"), home=env["HOME"],
                                 seconds=env.get("READBACK_JUDGE_SECONDS"))


def comparable(world, calls):
    """The provider calls with the value after `-o` (the answer file in a random folder) replaced by a name.

    The prompt stays as the bytes that the provider read.
    """
    result = [dict(call, args=list(call["args"])) for call in calls]
    for call in result:
        assert isinstance(call["prompt"], bytes)
        if "-o" in call["args"]:
            index = call["args"].index("-o") + 1
            assert re.fullmatch(re.escape(str(world.base / "tmp")) + r"/readback-judge-[A-Za-z0-9_]+/answer\.txt",
                                call["args"][index]), call["args"]
            call["args"][index] = "<answer file>"
    return result


def oracle_run(world, host, contract, handoff, repo, extra_env=None, interruptible=False):
    return run_helper(world, world.base / "oracle" / "outside_judge.py", host, contract, handoff, repo, extra_env,
                      interruptible=interruptible)


def split_receipt(out):
    """Return (the output bytes without their third line, the third line as bytes). Split at b"\n" only."""
    first, second, third, rest = out.split(b"\n", 3)
    return first + b"\n" + second + b"\n" + rest, third


def compare_with_oracle(test, old, new):
    """Bytes: the status, the error output and the output without the third line equal those of the oracle.

    Return the third line as text.
    """
    test.assertEqual(new.err, old.err)
    test.assertEqual(new.code, old.code)
    rest, third = split_receipt(new.out)
    test.assertTrue(third.startswith(b"receipt    "), third)
    test.assertEqual(rest, old.out)
    test.assertEqual(new.out.count(b"\nreceipt    "), 1)
    test.assertNotIn(b"receipt    ", old.out)
    return third.decode("utf-8")


def reviews_of(common):
    """{key: sorted file names} for the review folders below the Git common directory."""
    root = os.path.join(common, "readback", "reviews")
    if not os.path.isdir(root):
        return {}
    return {key: sorted(os.listdir(os.path.join(root, key))) for key in sorted(os.listdir(root))}


def expect_records(test, repo, run, contract_bytes, handoff_bytes, *, route, verdict, answer, exit_status,
                   models, window, before=None, after=None, expected_binding="ends-equal", duplicates=(),
                   validation=None, completed=True, line=True, model_match="UNVERIFIED"):
    """Check the stored files of a run against values computed here. Return the attempt id."""
    repo = Path(os.path.realpath(repo))
    common = str(repo / ".git")
    manifest = own_identity(repo)
    before = before or cap("ok", manifest)
    after = after or cap("ok", manifest)
    prompt = run.calls[0]["prompt"]
    rules_sha = sha(prompt.split(b"\n\nYou are the terminal judge.", 1)[0])
    version = {"file_sha256": sha(HELPER.read_bytes())}
    key = own_key(sha(contract_bytes), sha(handoff_bytes), rules_sha, route,
                  version, before["manifest_sha256"] if before["state"] == "ok" else None)
    listing = reviews_of(common)
    test.assertEqual(sorted(listing), [key])
    names = listing[key]
    started_names = [n for n in names if n.endswith(".started.json")]
    test.assertEqual(len(started_names), 1)
    attempt = started_names[0][:-len(".started.json")]
    test.assertRegex(attempt, r"^[0-9a-f]{32}$")
    wanted_names = [attempt + ".completed.json", attempt + ".started.json"] if completed else [attempt + ".started.json"]
    test.assertEqual(names, wanted_names)
    folder = os.path.join(common, "readback", "reviews", key)
    started = json.loads(Path(folder, attempt + ".started.json").read_text())
    created = started.get("created", "")
    test.assertRegex(created, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    stamp = calendar.timegm(time.strptime(created, "%Y-%m-%dT%H:%M:%SZ"))
    test.assertLessEqual(int(window[0]) - 1, stamp)
    test.assertLessEqual(stamp, window[1] + 1)
    head = git(repo, "rev-parse", "HEAD").strip()
    project_home = os.path.join(run.home, "logbook", repo.name)  # D3, D9: no settings file, no session id, tier gate
    host = "claude" if route["provider"] == "codex" else "codex"
    seconds = float(run.seconds)
    call = {"host": host, "session_id": None, "tier": "gate", "mode": "gate",
            "mode_source": {"kind": "project", "settings_source": "defaults"}, "project_home": project_home,
            "settings_digest": readback_settings.load(project_home)["digest"], "settings_source": "defaults",
            "deadline_seconds": int(seconds) if seconds == int(seconds) else seconds, "deadline_source": "env"}
    expected_started = {
        "schema_version": 1, "attempt_id": attempt, "review_key": key, "created": created,
        "repository": {"root_name": repo.name, "worktree_root": str(repo), "head": head, "branch": "main"},
        "capture_before": before,
        "contract": {"path": run.contract_path, "sha256": sha(contract_bytes)},
        "handoff": {"path": run.handoff_path, "sha256": sha(handoff_bytes)},
        "rules_sha256": rules_sha, "route": route, "helper_version": version, "call": call,
    }
    test.assertEqual(started, expected_started)
    markers = os.path.join(common, "readback", "by-manifest")
    if not completed:
        test.assertFalse(os.path.exists(markers))
        return attempt
    done = os.path.join(folder, attempt + ".completed.json")
    stored = json.loads(Path(done).read_text())
    validation = validation or validate_handoff.validate(  # D8: the text that the judge sees, with universal newlines
        *(item.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n") for item in (contract_bytes, handoff_bytes)))
    expected_completed = dict(expected_started)
    expected_completed.update({
        "capture_after": after, "binding": expected_binding, "validation": validation,
        "judge": {"verdict": verdict, "provider": route["provider"], "requested_model": route["model"],
                  "effort": route["effort"], "reported_models": models, "model_match": model_match},
        "answer_sha256": None if answer is None else sha(answer.encode("utf-8")),
        "exit_status": exit_status, "duplicates_of": sorted(duplicates),
    })
    test.assertEqual(stored, expected_completed)
    if after["state"] == "ok":
        test.assertEqual(file_map(markers), {os.path.join(after["manifest_sha256"], attempt): sha(b"")})
        marker = os.path.join(markers, after["manifest_sha256"], attempt)
        test.assertEqual(stat.S_IMODE(os.stat(marker).st_mode), 0o600)
    else:
        test.assertFalse(os.path.exists(markers))
    if line:
        _rest, third = split_receipt(run.out)
        third = third.decode("utf-8")
        test.assertEqual(third, "receipt    %s | %s | %s | %s" % (attempt, expected_binding, own_phrase(validation), done))
    return attempt


def make_inputs(folder, contract=CONTRACT_TEXT, handoff=HANDOFF_TEXT, newline="\n"):
    """Write the contract and the handoff below a symlinked folder. Return (path, bytes) pairs."""
    real = folder / "inputs"
    real.mkdir()
    link = folder / "via link"
    link.symlink_to(real)
    result = []
    for name, text in (("contract 'quoted'.md", contract), ("handoff `quoted`.md", handoff)):
        data = text.replace("\n", newline).encode("utf-8")
        (real / name).write_bytes(data)
        result.append((link / name, data))
    return result


def route_object(provider, source="defaults"):
    model, effort = ("fable", "high") if provider == "claude" else ("gpt-6-astra", "high")
    return {"provider": provider, "model": model, "effort": effort, "source": source}


# ----------------------------------------------------------------- HelperTests

class HelperTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="readback-helper-")
        self.addCleanup(temp.cleanup)
        self.world = new_world(Path(os.path.realpath(temp.name)))

    def case(self, host, **kwargs):
        """A fresh repository and inputs; returns (repo, contract, handoff, contract bytes, handoff bytes)."""
        folder = fresh_dir(self.world, "case")
        repo = make_repo(folder, "repo 'quoted' $(not-a-command)")
        (contract, c_bytes), (handoff, h_bytes) = make_inputs(folder, **kwargs)
        return repo, contract, handoff, c_bytes, h_bytes

    def both(self, host, repo, contract, handoff, extra_env=None):
        old = oracle_run(self.world, host, contract, handoff, repo, extra_env)
        new = run_helper(self.world, HELPER, host, contract, handoff, repo, extra_env)
        return old, new

    def test_the_comparison_with_the_oracle_can_fail_on_one_cr_byte(self):
        def run(out, err=b"", code=0):
            return types.SimpleNamespace(out=out, err=err, code=code)
        old = run(b"JUDGE      PASS | x\nexecution  y\n\nPASS a\nb\n")
        line = b"receipt    none | no provider call\n"
        good = run(b"JUDGE      PASS | x\nexecution  y\n" + line + b"\nPASS a\nb\n")
        self.assertEqual(compare_with_oracle(self, old, good), "receipt    none | no provider call")
        bad_outputs = (
            run(b"JUDGE      PASS | x\nexecution  y\n" + line + b"\nPASS a\r\nb\r\n"),
            run(b"JUDGE      PASS | x\r\nexecution  y\n" + line + b"\nPASS a\nb\n"),
            run(b"JUDGE      PASS | x\nexecution  y\n" + line + b"\nPASS a\nb\r"),
            run(b"JUDGE      PASS | x\nexecution  y\n" + line + b"\nPASS a\nb\n", err=b"warning\r"),
            run(b"JUDGE      PASS | x\nexecution  y\n" + line + b"\nPASS a\nb\n", code=3),
            run(b"JUDGE      PASS | x\nexecution  y\nreceipt   none\n\nPASS a\nb\n"),
        )
        for number, bad in enumerate(bad_outputs):
            with self.subTest(case=number):
                with self.assertRaises(AssertionError):
                    compare_with_oracle(self, old, bad)
        with self.assertRaises(AssertionError):
            compare_with_oracle(self, run(old.out, b"warning\n"), run(good.out, b"warning\r\n"))
        self.assertEqual(split_receipt(b"a\r\nb\nc\n\nd")[0], b"a\r\nb\n\nd")

    def test_the_fake_provider_records_the_prompt_bytes_without_loss(self):
        prompt = b"line one\r\nline two\rline three\nPASS \xc3\xa9 \xff\n"
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                record = self.world.base / ("rec-" + provider)
                env = dict(self.world.env, FAKE_RECORD=str(record), READBACK_JUDGE_ACTIVE="1")
                answer = self.world.base / "answer.txt"
                argv = ["exec", "-o", str(answer), "-"] if provider == "codex" else ["--output-format", "json",
                                                                                      "--model", "fable"]
                done = subprocess.run([str(self.world.base / "bin" / provider), *argv], input=prompt, env=env,
                                      capture_output=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)
                calls = [json.loads(line) for line in record.read_text().splitlines()]
                self.assertEqual(len(calls), 1)
                self.assertEqual(bytes.fromhex(calls[0]["prompt_hex"]), prompt)
                self.assertIn(b"\r\n", bytes.fromhex(calls[0]["prompt_hex"]))

    def test_pass_and_fail_equal_the_helper_at_bc63aae_and_store_the_hashes(self):
        for host, provider in HOSTS:
            for label, answer, verdict in (("PASS", "PASS Évidence ✓ supports the handoff.\nKeep this finding.\n", "PASS"),
                                           ("FAIL", FAIL_ANSWER, "FAIL")):
                with self.subTest(provider=provider, outcome=label):
                    repo, contract, handoff, c_bytes, h_bytes = self.case(host)
                    began = time.time()
                    old, new = self.both(host, repo, contract, handoff, {"FAKE_ANSWER": answer})
                    ended = time.time()
                    self.assertEqual((old.code, new.code), (0, 0))
                    self.assertEqual(len(old.calls), 1)
                    self.assertEqual(len(new.calls), 1)
                    self.assertEqual(old.calls[0]["prompt"], new.calls[0]["prompt"])
                    self.assertEqual(comparable(self.world, old.calls), comparable(self.world, new.calls))
                    compare_with_oracle(self, old, new)
                    new.contract_path, new.handoff_path = os.path.realpath(contract), os.path.realpath(handoff)
                    expect_records(self, repo, new, c_bytes, h_bytes, route=route_object(provider), verdict=verdict,
                                   answer=answer, exit_status=0, window=(began, ended),
                                   models=["claude-fable-5-2"] if provider == "claude" else "unattested")

    def test_provider_error_deadline_missing_command_and_unreadable_contract(self):
        outcomes = (
            ("provider exit code", {"FAKE_MODE": "auth_error"}, 1),
            ("missed deadline", {"FAKE_MODE": "sleep", "READBACK_JUDGE_SECONDS": "2"}, 1),
            ("missing command", {"PATH": None}, 0),
            ("unreadable contract", {"UNREADABLE": "1"}, 0),
        )
        for host, provider in HOSTS:
            for label, extra, started in outcomes:
                with self.subTest(provider=provider, outcome=label):
                    repo, contract, handoff, c_bytes, h_bytes = self.case(host)
                    extra = dict(extra)
                    if "PATH" in extra:
                        extra["PATH"] = str(self.world.base / "empty") + os.pathsep + str(self.world.base / "gitbin")
                    if extra.pop("UNREADABLE", None):
                        contract = Path(os.path.realpath(contract)).parent
                    began = time.time()
                    old, new = self.both(host, repo, contract, handoff, extra)
                    ended = time.time()
                    self.assertEqual((old.code, new.code), (3, 3))
                    self.assertEqual(len(old.calls), started)
                    self.assertEqual(len(new.calls), started)
                    third = compare_with_oracle(self, old, new)
                    self.assertEqual(comparable(self.world, old.calls), comparable(self.world, new.calls))
                    if not started:
                        self.assertEqual(third, "receipt    none | no provider call")
                        self.assertFalse(os.path.exists(repo / ".git" / "readback"))
                        continue
                    new.contract_path, new.handoff_path = os.path.realpath(contract), os.path.realpath(handoff)
                    expect_records(self, repo, new, c_bytes, h_bytes, route=route_object(provider), verdict="none",
                                   answer=None, exit_status=3, window=(began, ended), models="unattested")

    def test_a_missing_contract_or_handoff_and_an_empty_one_start_no_provider(self):
        for host, provider in HOSTS:
            with self.subTest(provider=provider):
                repo, contract, handoff, c_bytes, h_bytes = self.case(host)
                for contract_arg, handoff_arg in ((repo / "missing.md", handoff), (contract, repo / "missing.md")):
                    old, new = self.both(host, repo, contract_arg, handoff_arg)
                    self.assertEqual(old.calls + new.calls, [])
                    self.assertEqual(compare_with_oracle(self, old, new), "receipt    none | no provider call")
                empty = repo.parent / "empty.md"
                empty.write_bytes(b" \n\t\n")
                old, new = self.both(host, repo, empty, handoff)
                self.assertEqual(old.calls + new.calls, [])
                self.assertIn("must not be empty", new.text)
                self.assertEqual(compare_with_oracle(self, old, new), "receipt    none | no provider call")
                bad = repo.parent / "bad.md"
                bad.write_bytes(b"CON-1\n\xff\xfe\n")
                old, new = self.both(host, repo, bad, handoff)
                self.assertEqual(old.calls + new.calls, [])
                self.assertEqual(compare_with_oracle(self, old, new), "receipt    none | no provider call")
                self.assertFalse(os.path.exists(repo / ".git" / "readback"))

    def test_the_provider_gets_the_same_prompt_for_lf_crlf_and_cr_input(self):
        for host, provider in HOSTS:
            for newline, label, answer in (("\n", "LF", PASS_ANSWER), ("\r\n", "CRLF", PASS_ANSWER),
                                           ("\r\n", "CRLF", FAIL_ANSWER), ("\r", "CR", PASS_ANSWER)):
                with self.subTest(provider=provider, newline=label, answer=answer[:4]):
                    repo, contract, handoff, c_bytes, h_bytes = self.case(host, newline=newline)
                    for raw in (c_bytes, h_bytes):
                        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n") if newline == "\r\n" else 0)
                        self.assertEqual(raw.count(b"\r"), 0 if newline == "\n" else raw.count(newline.encode()))
                        self.assertEqual(raw.count(b"\n"), 0 if newline == "\r" else raw.count(b"\n"))
                        self.assertGreater(len(raw.split(newline.encode())), 3)
                    began = time.time()
                    old, new = self.both(host, repo, contract, handoff, {"FAKE_ANSWER": answer})
                    ended = time.time()
                    self.assertEqual(old.calls[0]["prompt"], new.calls[0]["prompt"])
                    self.assertNotIn(b"\r", new.calls[0]["prompt"])
                    self.assertNotIn(b"\r", old.calls[0]["prompt"])
                    self.assertIn(CONTRACT_TEXT.encode("utf-8"), new.calls[0]["prompt"])
                    self.assertIn(HANDOFF_TEXT.encode("utf-8"), new.calls[0]["prompt"])
                    self.assertEqual(comparable(self.world, old.calls), comparable(self.world, new.calls))
                    compare_with_oracle(self, old, new)
                    new.contract_path, new.handoff_path = os.path.realpath(contract), os.path.realpath(handoff)
                    expect_records(self, repo, new, c_bytes, h_bytes, route=route_object(provider),
                                   verdict=answer[:4], answer=answer, exit_status=0, window=(began, ended),
                                   models=["claude-fable-5-2"] if provider == "claude" else "unattested")

    def test_a_handoff_with_defects_gives_validation_false_n_on_the_line(self):
        # D8: structural validation `false` stops before the provider. The oracle helper still calls it.
        host, provider = HOSTS[0]
        repo, contract, handoff, c_bytes, h_bytes = self.case(host, handoff="id HND\nstate DONE\n")
        old, new = self.both(host, repo, contract, handoff)
        self.assertEqual((old.code, len(old.calls)), (0, 1))
        self.assertEqual((new.code, new.calls), (3, []))
        result = validate_handoff.validate(CONTRACT_TEXT, "id HND\nstate DONE\n")
        defects = len([f for f in result["findings"] if f["level"] == "defect"])
        self.assertGreater(defects, 5)
        lines = new.text.split("\n")
        self.assertTrue(lines[0].startswith("JUDGE      none | codex:gpt-6-astra | "), lines[0])
        self.assertTrue(lines[0].endswith(" | no verdict: the handoff fails structural validation (%d defects)" % defects))
        self.assertEqual(lines[2], "receipt    none | no provider call")
        self.assertEqual("\n".join(lines[3:]), "\n" + json.dumps(result, sort_keys=True, indent=2) + "\n")
        self.assertFalse(os.path.exists(os.path.join(os.path.realpath(repo), ".git", "readback")))

    def test_route_source_and_reported_models_that_equal_the_request(self):
        row = "judge | codex | claude | claude-fable-5-2 | medium\n"
        (self.world.base / "TEAMMATES.md").write_text(row)
        repo, contract, handoff, c_bytes, h_bytes = self.case("codex")
        began = time.time()
        old, new = self.both("codex", repo, contract, handoff, {"FAKE_MODEL": "claude-fable-5-2"})
        ended = time.time()
        self.assertNotIn("reported model", new.text)
        compare_with_oracle(self, old, new)
        new.contract_path, new.handoff_path = os.path.realpath(contract), os.path.realpath(handoff)
        route = {"provider": "claude", "model": "claude-fable-5-2", "effort": "medium", "source": "table"}
        expect_records(self, repo, new, c_bytes, h_bytes, route=route, verdict="PASS", answer=PASS_ANSWER,
                       exit_status=0, window=(began, ended), models=["claude-fable-5-2"], model_match="VERIFIED")
        (self.world.base / "TEAMMATES.md").write_text("judge | codex | claude | fable | low\n")
        repo, contract, handoff, c_bytes, h_bytes = self.case("codex")
        usage = {"FAKE_USAGE": json.dumps({"claude-fable-5-2": {}, "claude-fable-5-1": {}}),
                 "FAKE_MODEL": "claude-fable-5-0"}
        began = time.time()
        old, new = self.both("codex", repo, contract, handoff, usage)
        ended = time.time()
        self.assertIn("reported model: claude-fable-5-0, claude-fable-5-1, claude-fable-5-2", new.text)
        compare_with_oracle(self, old, new)
        new.contract_path, new.handoff_path = os.path.realpath(contract), os.path.realpath(handoff)
        route = {"provider": "claude", "model": "fable", "effort": "low", "source": "table"}
        expect_records(self, repo, new, c_bytes, h_bytes, route=route, verdict="PASS", answer=PASS_ANSWER,
                       exit_status=0, window=(began, ended),
                       models=["claude-fable-5-0", "claude-fable-5-1", "claude-fable-5-2"])

    def test_a_legacy_four_column_judge_row_gives_source_table(self):
        rows = {"claude": "judge | codex | gpt-6-astra | medium\n", "codex": "judge | claude | fable | medium\n"}
        for host, provider in HOSTS:
            with self.subTest(provider=provider):
                (self.world.base / "TEAMMATES.md").write_text(rows[host])
                repo, contract, handoff, c_bytes, h_bytes = self.case(host)
                began = time.time()
                old, new = self.both(host, repo, contract, handoff)
                ended = time.time()
                self.assertIn("execution  %s | " % provider, new.text)
                self.assertIn(" | medium | cli | none", new.text)
                compare_with_oracle(self, old, new)
                self.assertEqual((old.code, new.code, len(old.calls), len(new.calls)), (0, 0, 1, 1))
                new.contract_path, new.handoff_path = os.path.realpath(contract), os.path.realpath(handoff)
                route = dict(route_object(provider, "table"), effort="medium")
                expect_records(self, repo, new, c_bytes, h_bytes, route=route, verdict="PASS", answer=PASS_ANSWER,
                               exit_status=0, window=(began, ended),
                               models=["claude-fable-5-2"] if provider == "claude" else "unattested")
                key = next(iter(reviews_of(str(repo / ".git"))))
                for name in reviews_of(str(repo / ".git"))[key]:
                    stored = json.loads(Path(repo, ".git", "readback", "reviews", key, name).read_text())
                    self.assertEqual(stored["route"]["source"], "table")

    def test_a_second_run_under_the_same_key_lists_the_first_as_a_duplicate(self):
        host, provider = HOSTS[0]
        repo, contract, handoff, c_bytes, h_bytes = self.case(host)
        first = run_helper(self.world, HELPER, host, contract, handoff, repo)
        second = run_helper(self.world, HELPER, host, contract, handoff, repo)
        third_run = run_helper(self.world, HELPER, host, contract, handoff, repo)
        common = str(repo / ".git")
        listing = reviews_of(common)
        self.assertEqual(len(listing), 1)
        names = next(iter(listing.values()))
        ids = sorted(n[:-len(".completed.json")] for n in names if n.endswith(".completed.json"))
        self.assertEqual(len(ids), 3)
        lines = [split_receipt(run.out)[1].decode("utf-8").split(" | ")[0][len("receipt    "):]
                 for run in (first, second, third_run)]
        self.assertEqual(sorted(lines), ids)
        folder = os.path.join(common, "readback", "reviews", next(iter(listing)))
        for position, attempt in enumerate(lines):
            stored = json.loads(Path(folder, attempt + ".completed.json").read_text())
            self.assertEqual(stored["duplicates_of"], sorted(lines[:position]))
        self.assertEqual(review_receipt.list_unfinished(common), {"indeterminate": [], "partial": []})

    def test_a_linked_worktree_stores_below_the_main_common_directory(self):
        host, provider = HOSTS[1]
        folder = fresh_dir(self.world, "linked")
        main = make_repo(folder, "main")
        linked = folder / "linked"
        git(main, "worktree", "add", "-q", "-b", "side", str(linked))
        (contract, c_bytes), (handoff, h_bytes) = make_inputs(folder)
        worktree_before = {k: v for k, v in file_map(linked).items() if not k.startswith(".git")}
        old = oracle_run(self.world, host, contract, handoff, linked)
        new = run_helper(self.world, HELPER, host, contract, handoff, linked)
        compare_with_oracle(self, old, new)
        common = os.path.realpath(main / ".git")
        listing = reviews_of(common)
        self.assertEqual(len(listing), 1)
        _rest, third = split_receipt(new.out)
        third = third.decode("utf-8")
        folder_of = os.path.join(common, "readback", "reviews", next(iter(listing)))
        self.assertTrue(third.endswith(" | " + os.path.join(folder_of, third.split(" | ")[0][11:] + ".completed.json")))
        self.assertFalse(any("readback" in name for name in file_map(os.path.join(common, "worktrees"))))
        self.assertEqual(worktree_before, {k: v for k, v in file_map(linked).items() if not k.startswith(".git")})
        self.assertEqual(sorted(os.listdir(linked)), [".git", "a.txt", "link", "run.sh", "sub"])
        stored = json.loads(Path(folder_of, listing[next(iter(listing))][0]).read_text())
        self.assertEqual(stored["repository"]["root_name"], "linked")
        self.assertEqual(stored["repository"]["branch"], "side")
        self.assertEqual(stored["repository"]["worktree_root"], os.path.realpath(linked))

    def test_repository_fields_for_a_detached_head_and_a_repository_without_a_commit(self):
        host, provider = HOSTS[0]
        repo, contract, handoff, c_bytes, h_bytes = self.case(host)
        git(repo, "checkout", "-q", "--detach")
        new = run_helper(self.world, HELPER, host, contract, handoff, repo)
        common = str(repo / ".git")
        key = next(iter(reviews_of(common)))
        started = [n for n in reviews_of(common)[key] if n.endswith(".started.json")][0]
        stored = json.loads(Path(common, "readback", "reviews", key, started).read_text())
        self.assertEqual(stored["repository"], {
            "root_name": repo.name, "worktree_root": os.path.realpath(repo),
            "head": git(repo, "rev-parse", "HEAD").strip(), "branch": None})
        folder = fresh_dir(self.world, "unborn")
        empty = folder / "unborn repo"
        empty.mkdir()
        git(empty, "init", "-q", "-b", "trunk")
        (empty / "x.txt").write_text("x\n")
        old = oracle_run(self.world, host, contract, handoff, empty)
        new = run_helper(self.world, HELPER, host, contract, handoff, empty)
        compare_with_oracle(self, old, new)
        common = str(empty / ".git")
        key = next(iter(reviews_of(common)))
        started = [n for n in reviews_of(common)[key] if n.endswith(".started.json")][0]
        stored = json.loads(Path(common, "readback", "reviews", key, started).read_text())
        self.assertEqual(stored["repository"], {
            "root_name": "unborn repo", "worktree_root": os.path.realpath(empty), "head": None, "branch": "trunk"})
        self.assertEqual(stored["capture_before"], cap("ok", own_identity(empty)))


# ----------------------------------------------------------------- FaultTests

DRIVER = r'''
import errno, hashlib, json, os, signal, sys, types, uuid
spec = json.loads(os.environ["FAULT_SPEC"])
sys.path.insert(0, spec["scripts"])
import review_capture, review_receipt, validate_handoff
log = open(spec["log"], "a")


def note(*item):
    log.write(json.dumps(item) + "\n")
    log.flush()


captures = []
real_capture = review_capture.capture


def capture(repo, seconds):
    captures.append(1)
    note("capture", os.fspath(repo), seconds)
    mode = spec["capture"].get(str(len(captures)))
    if mode is None:
        return real_capture(repo, seconds)
    if mode == "raise":
        raise RuntimeError("injected")
    if mode == "KeyboardInterrupt":
        raise KeyboardInterrupt()
    if mode == "InterruptedError":
        raise InterruptedError("injected")
    return {"manifest_version": 1, "state": mode, "reason": "injected " + mode, "manifest_sha256": None,
            "entries": None, "index_sha256": None, "root": None}


review_capture.capture = capture
if spec.get("validator") == "raise":
    def validate(contract_text, handoff_text):
        note("validate", hashlib.sha256(contract_text.encode("utf-8")).hexdigest(),
             hashlib.sha256(handoff_text.encode("utf-8")).hexdigest())
        raise RuntimeError("injected")
    validate_handoff.validate = validate
if spec.get("uuid"):
    uuid.uuid4 = lambda: types.SimpleNamespace(hex=spec["uuid"])
writes = []
real_write = review_receipt._write


def write(fd, data):
    writes.append(1)
    note("write", len(data))
    if spec.get("write_fail") == len(writes):
        os.write(fd, data[: len(data) // 2])
        os.close(fd)
        raise OSError(errno.ENOSPC, "injected")
    return real_write(fd, data)


review_receipt._write = write
for name in spec.get("block", []):
    sys.modules[name] = None
import outside_judge
signal.signal(signal.SIGTERM, outside_judge.interrupted)
sys.exit(outside_judge.main())
'''

UUID = "ab" * 16


def driver_run(world, host, contract, handoff, repo, spec, extra_env=None):
    """Run the helper through the driver, which injects the fault of `spec`. Return the run and its log."""
    folder = fresh_dir(world, "driver")
    driver = folder / "driver.py"
    driver.write_text(DRIVER)
    log = folder / "log.jsonl"
    spec = dict(spec, scripts=str(SCRIPTS), log=str(log), capture=spec.get("capture", {}))
    env = dict(extra_env or {})
    env["FAULT_SPEC"] = json.dumps(spec)
    run = run_helper(world, None, host, contract, handoff, repo, env, command=[sys.executable, "-B", str(driver)])
    run.log = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return run


class FaultTests(unittest.TestCase):
    """Each fault is injected into a run that otherwise gives PASS, for both providers.

    Every case has its own repository. The prepared files are made first, then the helper of bc63aae runs,
    then the helper under test runs in the same repository with the same environment.
    """

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="readback-fault-")
        cls.world = new_world(Path(os.path.realpath(cls.temp.name)))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setup_case(self, host, git_repo=True):
        folder = fresh_dir(self.world, "case")
        (contract, c_bytes), (handoff, h_bytes) = make_inputs(folder)
        if git_repo:
            repo = make_repo(folder, "repo")
        else:
            repo = folder / "repo"
            repo.mkdir()
            (repo / "a.txt").write_text("alpha\n")
        return types.SimpleNamespace(host=host, repo=repo, contract=contract, handoff=handoff, c_bytes=c_bytes,
                                     h_bytes=h_bytes, common=str(repo / ".git"),
                                     contract_path=os.path.realpath(contract), handoff_path=os.path.realpath(handoff))

    def execute(self, case, spec=None, prepare=None, extra_env=None, interruptible=False, reaches_provider=True):
        """Prepare, run the oracle, then run the helper under test. Return (run, receipt line, files before).

        With reaches_provider false (D8: validation cannot run) the helper makes no provider call and its
        output is not compared with the oracle's.
        """
        if prepare:
            prepare(case)
        old = oracle_run(self.world, case.host, case.contract, case.handoff, case.repo, extra_env, interruptible)
        before = file_map(case.common)
        if interruptible:
            run = run_helper(self.world, HELPER, case.host, case.contract, case.handoff, case.repo, extra_env,
                             interruptible=True)
        else:
            run = driver_run(self.world, case.host, case.contract, case.handoff, case.repo, spec or {}, extra_env)
        self.assertEqual(len(old.calls), 1)
        self.assertEqual(len(run.calls), 1 if reaches_provider else 0)
        third = compare_with_oracle(self, old, run) if reaches_provider else run.text.split("\n")[2]
        run.contract_path, run.handoff_path = case.contract_path, case.handoff_path
        run.oracle = old
        return run, third, before

    def prepared(self, host):
        """The review key and the manifest identity that a run of this fixture gets."""
        probe = self.setup_case(host)
        old = oracle_run(self.world, host, probe.contract, probe.handoff, probe.repo)
        rules = sha(old.calls[0]["prompt"].split(b"\n\nYou are the terminal judge.", 1)[0])
        manifest = own_identity(probe.repo)
        key = own_key(sha(probe.c_bytes), sha(probe.h_bytes), rules, route_object(
            "codex" if host == "claude" else "claude"), {"file_sha256": sha(HELPER.read_bytes())}, manifest)
        return key, manifest

    def models(self, host):
        return ["claude-fable-5-2"] if host == "codex" else "unattested"

    def captures(self, run, case):
        return [entry for entry in run.log if entry[0] == "capture"], [["capture", os.path.realpath(case.repo), 5]]

    def test_the_validator_raises_or_cannot_be_imported(self):
        for host, provider in HOSTS:
            for label, spec, reason in (("raises", {"validator": "raise"}, "validator raised"),
                                        ("import fails", {"block": ["validate_handoff"]}, "validator not importable")):
                with self.subTest(provider=provider, fault=label):
                    case = self.setup_case(host)
                    began = time.time()
                    run, third, _before = self.execute(case, spec, reaches_provider=False)
                    # D8: validation that is `unavailable` blocks the provider call and stores no record
                    self.assertEqual(run.code, 3)
                    self.assertTrue(run.text.split("\n")[0].endswith(" | no verdict: the structural validator is not available"))
                    self.assertEqual(third, "receipt    none | no provider call")
                    self.assertFalse(os.path.exists(case.common + "/readback"))
                    if label == "raises":
                        self.assertEqual([e for e in run.log if e[0] == "validate"],
                                         [["validate", sha(case.c_bytes), sha(case.h_bytes)]])

    def test_a_capture_raises_or_is_not_ok(self):
        raised = cap("error", None, "capture raised")
        for host, provider in HOSTS:
            faults = [("first raises", {"1": "raise"}, raised, None, "unbound: before error"),
                      ("second raises", {"2": "raise"}, None, raised, "unbound: after error")]
            for state in ("unsupported", "timeout", "error"):
                faults.append(("first " + state, {"1": state}, cap(state, None, "injected " + state), None,
                               "unbound: before " + state))
                faults.append(("second " + state, {"2": state}, None, cap(state, None, "injected " + state),
                               "unbound: after " + state))
            for label, captures, before, after, binding in faults:
                with self.subTest(provider=provider, fault=label):
                    case = self.setup_case(host)
                    began = time.time()
                    run, third, _before = self.execute(case, {"capture": captures})
                    ended = time.time()
                    self.assertEqual(run.code, 0)
                    seen, wanted = self.captures(run, case)
                    self.assertEqual(seen, wanted * 2)
                    expect_records(self, case.repo, run, case.c_bytes, case.h_bytes, route=route_object(provider),
                                   verdict="PASS", answer=PASS_ANSWER, exit_status=0, window=(began, ended),
                                   models=self.models(host), before=before, after=after, expected_binding=binding,
                                   validation=validate_handoff.validate(CONTRACT_TEXT, HANDOFF_TEXT))
                    self.assertEqual(third.split(" | ")[1], binding)

    def test_modules_not_available_and_not_a_git_worktree(self):
        for host, provider in HOSTS:
            for blocked in ("review_receipt", "review_capture"):
                with self.subTest(provider=provider, blocked=blocked):
                    case = self.setup_case(host)
                    stops = blocked == "review_receipt"  # D8: without review_receipt validation is unavailable
                    run, third, before = self.execute(case, {"block": [blocked]}, reaches_provider=not stops)
                    self.assertEqual(third, "receipt    none | no provider call" if stops else "receipt    none | modules not available")
                    self.assertEqual(run.code, 3 if stops else 0)
                    self.assertEqual(file_map(case.common), before)
                    self.assertFalse(os.path.exists(case.common + "/readback"))
                    self.assertEqual([e for e in run.log if e[0] == "capture"], [])
            with self.subTest(provider=provider, fault="not a Git worktree"):
                case = self.setup_case(host, git_repo=False)
                probe = subprocess.run(["git", "-C", str(case.repo), "rev-parse", "--is-inside-work-tree"],
                                       env=clean_env(), capture_output=True, text=True)
                self.assertNotEqual(probe.returncode, 0)
                before = file_map(case.repo)
                run, third, _before = self.execute(case, {})
                self.assertEqual(third, "receipt    none | not a Git worktree")
                self.assertEqual(run.code, 0)
                self.assertEqual(file_map(case.repo), before)
                self.assertEqual(sorted(os.listdir(case.repo)), ["a.txt"])
                self.assertEqual([e for e in run.log if e[0] == "capture"], [])

    def test_a_bare_repository_and_a_git_folder_are_not_a_worktree(self):
        for host, provider in HOSTS:
            for label in ("bare repository", "git folder of a repository"):
                with self.subTest(provider=provider, folder=label):
                    case = self.setup_case(host)
                    if label == "bare repository":
                        case.repo = case.repo.parent / "bare"
                        git(case.repo.parent, "init", "-q", "--bare", "-b", "main", str(case.repo))
                    else:
                        case.repo = case.repo / ".git"
                    probe = subprocess.run(["git", "-C", str(case.repo), "rev-parse", "--is-inside-work-tree"],
                                           env=clean_env(), capture_output=True, text=True)
                    self.assertNotEqual(probe.stdout.strip(), "true")
                    common = subprocess.run(["git", "-C", str(case.repo), "rev-parse", "--git-common-dir"],
                                            env=clean_env(), capture_output=True, text=True)
                    self.assertEqual(common.returncode, 0)
                    self.assertEqual(review_capture.git_common_dir(case.repo), os.path.realpath(case.repo))
                    self.assertIsNone(review_receipt.repository_info(case.repo))
                    case.common = str(case.repo)
                    before = file_map(case.repo)
                    run, third, _before = self.execute(case, {})
                    self.assertEqual(third, "receipt    none | not a Git worktree")
                    self.assertEqual(run.code, 0)
                    self.assertEqual(file_map(case.repo), before)
                    self.assertFalse(os.path.exists(os.path.join(case.repo, "readback")))
                    self.assertEqual([e for e in run.log if e[0] == "capture"], [])

    def test_the_started_record_cannot_be_stored(self):
        sentinel = b"this started file existed before the run"
        for host, provider in HOSTS:
            key, manifest = self.prepared(host)

            def final_name_exists(c):
                folder = os.path.join(c.common, "readback", "reviews", key)
                os.makedirs(folder)
                Path(folder, UUID + ".started.json").write_bytes(sentinel)
            causes = (
                ("the folder cannot be written", {},
                 lambda c: Path(c.common, "readback").write_bytes(b"a file where a folder should be")),
                ("the final name exists", {"uuid": UUID}, final_name_exists),
                ("the write fails part way", {"write_fail": 1}, lambda c: None),
            )
            for label, spec, prepare in causes:
                with self.subTest(provider=provider, cause=label):
                    case = self.setup_case(host)
                    run, third, before = self.execute(case, spec, prepare)
                    self.assertEqual(third, "receipt    none | cannot write the record")
                    self.assertEqual(run.code, 0)
                    self.assertEqual(file_map(case.common), before)
                    seen, wanted = self.captures(run, case)
                    self.assertEqual(seen, wanted * 2)
                    if label == "the final name exists":
                        folder = os.path.join(case.common, "readback", "reviews", key)
                        self.assertEqual(Path(folder, UUID + ".started.json").read_bytes(), sentinel)
                        self.assertEqual(os.listdir(folder), [UUID + ".started.json"])

    def test_the_completed_record_or_the_marker_cannot_be_stored(self):
        for host, provider in HOSTS:
            key, manifest = self.prepared(host)

            def completed_exists(c):
                folder = os.path.join(c.common, "readback", "reviews", key)
                os.makedirs(folder)
                Path(folder, UUID + ".completed.json").write_bytes(b"old completed")

            def marker_exists(c):
                folder = os.path.join(c.common, "readback", "by-manifest", manifest)
                os.makedirs(folder)
                Path(folder, UUID).write_bytes(b"old marker")
            for label, spec, prepare, stored in (
                    ("completed: the final name exists", {"uuid": UUID}, completed_exists, False),
                    ("completed: the write fails part way", {"write_fail": 2}, None, False),
                    ("marker: the final name exists", {"uuid": UUID}, marker_exists, True),
                    ("marker: the write fails part way", {"write_fail": 3}, None, True)):
                with self.subTest(provider=provider, cause=label):
                    case = self.setup_case(host)
                    run, third, before = self.execute(case, spec, prepare)
                    self.assertEqual(run.code, 0)
                    after = file_map(case.common)
                    self.assertEqual({name: after[name] for name in before}, before)
                    new_files = sorted(set(after) - set(before))
                    folder = os.path.join(case.common, "readback", "reviews", key)
                    names = sorted(os.listdir(folder))
                    attempt = [n for n in names if n.endswith(".started.json")][0][:-len(".started.json")]
                    if spec.get("uuid"):
                        self.assertEqual(attempt, UUID)
                    prefix = "readback/reviews/" + key + "/"
                    seen, wanted = self.captures(run, case)
                    self.assertEqual(seen, wanted * 2)
                    if not stored:
                        self.assertEqual(third, "receipt    none | cannot write the record")
                        self.assertEqual(new_files, [prefix + attempt + ".started.json"])
                        started = json.loads(Path(folder, attempt + ".started.json").read_text())
                        self.assertEqual(started["attempt_id"], attempt)
                        if "final name" in label:
                            self.assertEqual(Path(folder, UUID + ".completed.json").read_bytes(), b"old completed")
                            self.assertEqual(names, [UUID + ".completed.json", UUID + ".started.json"])
                        else:
                            self.assertEqual(names, [attempt + ".started.json"])
                        self.assertFalse(any("by-manifest" in name for name in after))
                    else:
                        done = os.path.join(folder, attempt + ".completed.json")
                        self.assertEqual(third, "receipt    %s | ends-equal | validation true | %s" % (attempt, done))
                        self.assertEqual(new_files, [prefix + attempt + ".completed.json",
                                                     prefix + attempt + ".started.json"])
                        self.assertEqual(json.loads(Path(done).read_text())["attempt_id"], attempt)
                        markers = [n for n in after if "by-manifest" in n]
                        if "final name" in label:
                            marker = os.path.join(case.common, "readback", "by-manifest", manifest, UUID)
                            self.assertEqual(Path(marker).read_bytes(), b"old marker")
                            self.assertEqual(markers, ["readback/by-manifest/" + manifest + "/" + UUID])
                        else:
                            self.assertEqual(markers, [])

    def test_an_interruption_during_the_provider_call_keeps_the_started_record(self):
        for host, provider in HOSTS:
            with self.subTest(provider=provider):
                case = self.setup_case(host)
                run, third, _before = self.execute(case, extra_env={"FAKE_MODE": "interrupt_parent"},
                                                   interruptible=True)
                self.assertIn("no verdict: the judge was interrupted", run.oracle.text)
                self.assertEqual((run.oracle.code, run.code), (3, 3))
                self.assertEqual(third, "receipt    none | interrupted")
                expect_records(self, case.repo, run, case.c_bytes, case.h_bytes, route=route_object(provider),
                               verdict=None, answer=None, exit_status=None, window=(time.time() - 60, time.time()),
                               models=None, completed=False)
                self.assertFalse(os.path.exists(os.path.join(case.common, "readback", "by-manifest")))

    def test_an_interruption_inside_a_capture_propagates(self):
        for host, provider in HOSTS:
            for label, captures, started_provider, records in (
                    ("first capture, InterruptedError", {"1": "InterruptedError"}, 0, False),
                    ("first capture, KeyboardInterrupt", {"1": "KeyboardInterrupt"}, 0, False),
                    ("second capture, InterruptedError", {"2": "InterruptedError"}, 1, True),
                    ("second capture, KeyboardInterrupt", {"2": "KeyboardInterrupt"}, 1, True)):
                with self.subTest(provider=provider, interruption=label):
                    case = self.setup_case(host)
                    run = driver_run(self.world, host, case.contract, case.handoff, case.repo, {"capture": captures})
                    route = route_object(provider)
                    head = git(case.repo, "rev-parse", "--short", "HEAD").strip()
                    self.assertEqual(run.out, ("JUDGE      none | %s:%s | repo@%s | no verdict: the judge was interrupted\n"
                                     "execution  %s | %s | %s | cli | none\nreceipt    none | interrupted\n"
                                     % (provider, route["model"], head, provider, route["model"], route["effort"])
                                     ).encode("utf-8"))
                    self.assertEqual((run.code, len(run.calls)), (3, started_provider))
                    files = sorted(file_map(os.path.join(case.common, "readback")))
                    if records:
                        self.assertEqual(len(files), 1)
                        self.assertTrue(files[0].endswith(".started.json"))
                    else:
                        self.assertEqual(files, [])


# ------------------------------------------------------- SyncAndSnapshotTests

SNAPSHOT = ROOT / "checks" / "judge_snapshot.py"
SYNCED = ("review_receipt.py", "review_capture.py", "validate_handoff.py")


def snapshot_lines(root):
    result = subprocess.run([sys.executable, "-B", str(SNAPSHOT), str(root)], capture_output=True, timeout=30)
    assert result.returncode == 0 and result.stderr == b"", (result.returncode, result.stderr)
    assert result.stdout.endswith(b"\n") and b"\r" not in result.stdout
    return result.stdout.decode("utf-8").split("\n")[:-1]


class SyncAndSnapshotTests(unittest.TestCase):
    def test_the_portable_skill_holds_the_three_modules_byte_equal(self):
        for name in SYNCED:
            with self.subTest(module=name):
                source = (ROOT / "scripts" / name).read_bytes()
                copy = (ROOT / "portable" / "skills" / "readback" / "scripts" / name).read_bytes()
                self.assertGreater(len(source), 1000)
                self.assertEqual(copy, source)
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "sync.py"), "--check"], cwd=ROOT,
                                capture_output=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"portable skills are current\n", b""))
        listed = (ROOT / "scripts" / "sync.py").read_text()
        for name in SYNCED:
            self.assertIn('"' + name + '"', listed)

    def test_the_live_check_calls_the_snapshot_helper(self):
        text = (ROOT / "checks" / "outside-judge.sh").read_bytes().decode("utf-8")
        found = re.search(r"^snapshot\(\) \{\n(.*?)^\}\n", text, re.S | re.M)
        self.assertIsNotNone(found)
        self.assertEqual(found.group(1), '  python3 "$HERE/checks/judge_snapshot.py" "$TMP"\n')
        self.assertNotIn("hashlib", text)
        self.assertNotIn("<<'PY'", text)
        self.assertEqual(text.count("snapshot > "), 2)
        with tempfile.TemporaryDirectory(prefix="readback-snap-") as temp:
            tmp = Path(os.path.realpath(temp))
            (tmp / "bin").mkdir()
            (tmp / "bin" / "python3").symlink_to(sys.executable)
            (tmp / "judge-a" / "work").mkdir(parents=True)
            (tmp / "judge-a" / "work" / "f.txt").write_bytes(b"one")
            script = 'HERE="$1"; TMP="$2"\n' + text[found.start():found.end()] + "snapshot\n"
            result = subprocess.run(["/bin/bash", "-c", script, "bash", str(ROOT), str(tmp)],
                                    env={"PATH": str(tmp / "bin")}, capture_output=True, timeout=30)
            self.assertEqual((result.returncode, result.stderr), (0, b""))
            self.assertEqual(result.stdout, ("judge-a/work/f.txt " + sha(b"one") + "\n").encode("utf-8"))

    def tree(self, temp):
        root = Path(os.path.realpath(temp))
        work = root / "judge-a" / "work"
        (work / "d").mkdir(parents=True)
        (work / "ren").mkdir()
        (work / ".git").mkdir()
        (work / "f.txt").write_bytes(b"one")
        (work / "g.txt").write_bytes(b"gee")
        (work / "d" / "inner.txt").write_bytes(b"inner")
        (work / "ren" / "x.txt").write_bytes(b"ex")
        (work / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        os.symlink("f.txt", work / "ln")
        (root / "judge-b" / "work").mkdir(parents=True)
        (root / "judge-b" / "work" / "h.txt").write_bytes(b"aitch")
        (root / "other" / "work").mkdir(parents=True)
        (root / "other" / "work" / "z.txt").write_bytes(b"zed")
        return root, work

    def test_a_comparison_reports_each_change_outside_git_readback(self):
        def line(path, value):
            return path + " " + value
        one, gee = sha(b"one"), sha(b"gee")
        cases = []

        def changed_bytes(work):
            (work / "f.txt").write_bytes(b"two")
        cases.append(("changed file bytes", changed_bytes,
                      [line("judge-a/work/f.txt", one)], [line("judge-a/work/f.txt", sha(b"two"))]))

        def changed_link(work):
            (work / "ln").unlink()
            os.symlink("g.txt", work / "ln")
        cases.append(("changed symlink target", changed_link,
                      [line("judge-a/work/ln", "link:f.txt")], [line("judge-a/work/ln", "link:g.txt")]))

        def renamed(work):
            (work / "ren").rename(work / "ren2")
        cases.append(("renamed folder", renamed,
                      [line("judge-a/work/ren", "directory"), line("judge-a/work/ren/x.txt", sha(b"ex"))],
                      [line("judge-a/work/ren2", "directory"), line("judge-a/work/ren2/x.txt", sha(b"ex"))]))

        def add_file(work):
            (work / "new.txt").write_bytes(b"new")
        cases.append(("added file", add_file, [], [line("judge-a/work/new.txt", sha(b"new"))]))

        def add_link(work):
            os.symlink("nowhere", work / "newln")
        cases.append(("added symlink", add_link, [], [line("judge-a/work/newln", "link:nowhere")]))

        def add_folder(work):
            (work / "newdir").mkdir()
        cases.append(("added folder", add_folder, [], [line("judge-a/work/newdir", "directory")]))

        def delete_file(work):
            (work / "g.txt").unlink()
        cases.append(("deleted file", delete_file, [line("judge-a/work/g.txt", gee)], []))

        def delete_link(work):
            (work / "ln").unlink()
        cases.append(("deleted symlink", delete_link, [line("judge-a/work/ln", "link:f.txt")], []))

        def delete_folder(work):
            shutil.rmtree(work / "ren")
        cases.append(("deleted folder", delete_folder,
                      [line("judge-a/work/ren", "directory"), line("judge-a/work/ren/x.txt", sha(b"ex"))], []))

        def siblings(work):
            (work / ".git" / "readback2").mkdir()
            (work / ".git" / "readback2" / "k").write_bytes(b"k")
            (work / ".git" / "readbackx").write_bytes(b"x")
            (work / ".git" / "other").write_bytes(b"o")
            (work / "readback").mkdir()
            (work / "d" / ".git").mkdir()
            (work / "d" / ".git" / "readback").mkdir()
            (work / "d" / ".git" / "readback" / "deep").write_bytes(b"deep")
        cases.append(("paths that only look like .git/readback", siblings, [], sorted([
            line("judge-a/work/.git/readback2", "directory"), line("judge-a/work/.git/readback2/k", sha(b"k")),
            line("judge-a/work/.git/readbackx", sha(b"x")), line("judge-a/work/.git/other", sha(b"o")),
            line("judge-a/work/readback", "directory"), line("judge-a/work/d/.git", "directory"),
            line("judge-a/work/d/.git/readback", "directory"), line("judge-a/work/d/.git/readback/deep", sha(b"deep"))])))

        for label, change, removed, added in cases:
            with self.subTest(change=label):
                with tempfile.TemporaryDirectory(prefix="readback-snap-") as temp:
                    root, work = self.tree(temp)
                    before = snapshot_lines(root)
                    self.assertIn(line("judge-a/work/f.txt", one), before)
                    self.assertIn(line("judge-b/work/h.txt", sha(b"aitch")), before)
                    self.assertFalse(any(l.startswith("other/") for l in before))
                    change(work)
                    after = snapshot_lines(root)
                    self.assertEqual(sorted(set(before) - set(after)), sorted(removed))
                    self.assertEqual(sorted(set(after) - set(before)), sorted(added))
                    self.assertNotEqual(before, after)

    def test_a_comparison_never_reports_git_readback_or_anything_below_it(self):
        with tempfile.TemporaryDirectory(prefix="readback-snap-") as temp:
            root, work = self.tree(temp)
            before = snapshot_lines(root)
            self.assertFalse(any("readback" in l for l in before))
            readback = work / ".git" / "readback"
            steps = []

            def create():
                (readback / "reviews" / "key").mkdir(parents=True)
                (readback / "reviews" / "key" / "a.started.json").write_bytes(b"{}")
                (readback / "by-manifest").mkdir()
                os.symlink("reviews", readback / "ln")
            steps.append(create)
            steps.append(lambda: (readback / "reviews" / "key" / "a.started.json").write_bytes(b'{"x": 1}'))
            steps.append(lambda: (readback / "reviews" / "key" / "b.completed.json").write_bytes(b"{}"))
            steps.append(lambda: (readback / "ln").unlink())
            steps.append(lambda: (readback / "reviews" / "key" / "a.started.json").unlink())
            steps.append(lambda: shutil.rmtree(readback / "reviews"))
            steps.append(lambda: shutil.rmtree(readback))
            steps.append(lambda: os.symlink("../g", readback))
            steps.append(lambda: readback.unlink())
            steps.append(lambda: readback.write_bytes(b"a file"))
            steps.append(lambda: readback.unlink())
            for number, step in enumerate(steps):
                step()
                with self.subTest(step=number):
                    after = snapshot_lines(root)
                    self.assertEqual(after, before)
                    self.assertFalse(any("readback" in l for l in after))
            self.assertEqual(("\n".join(before) + "\n").encode("utf-8"), subprocess.run(
                [sys.executable, "-B", str(SNAPSHOT), str(root)], capture_output=True).stdout)

    def test_the_helper_prints_what_the_inline_snapshot_of_bc63aae_printed(self):
        old = subprocess.run(["git", "-C", str(ROOT), "show", BASELINE + ":checks/outside-judge.sh"],
                             env=clean_env(), capture_output=True, check=True, timeout=30).stdout
        code = old.split(b"python3 - \"$TMP\" <<'PY'\n", 1)[1].split(b"\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory(prefix="readback-snap-") as temp:
            root, work = self.tree(temp)
            os.symlink("g.txt", work / "d" / "second link")
            result = subprocess.run([sys.executable, "-B", "-", str(root)], input=code, capture_output=True,
                                    timeout=30)
            self.assertEqual((result.returncode, result.stderr), (0, b""))
            self.assertGreater(len(result.stdout.split(b"\n")), 8)
            self.assertEqual(result.stdout.decode("utf-8").split("\n")[:-1], snapshot_lines(root))


if __name__ == "__main__":
    unittest.main(failfast=True)
