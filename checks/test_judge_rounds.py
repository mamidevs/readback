#!/usr/bin/env python3
"""Offline checks for scripts/judge_rounds.py and its wiring in scripts/outside_judge.py (CON-20261118-17).

Run one class: python3 -B checks/test_judge_rounds.py LedgerTests
Classes: LedgerTests (Q1 to Q12, F1 to F6, pure functions), AuthorizationTests (A1 to A12), RaceTests (X1 to X6,
real processes), ChainTests (L1 to L8). Test names start with the row: test_q01_..., test_x04_...

Ledger, authorization and chain rows call `outside_judge.main` in this process with a fake `run_process` and a clock
that the test sets. Race rows start real helper processes and a fake provider that waits or hangs. Every helper run
sets HOME to a temporary folder and removes both session variables unless a row sets one. No model call, no network.
"""

import calendar
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_judge_modes as base  # noqa: E402  (JudgeCase and its fixtures)

outside_judge, readback_session, readback_settings = base.outside_judge, base.readback_session, base.readback_settings
import judge_rounds  # noqa: E402

HELPER = base.HELPER
LIN = "CON-20261117-01"
T0 = calendar.timegm((2026, 11, 18, 10, 0, 0))
PASS = "PASS Evidence supports the handoff.\n"
FAIL = "FAIL The handoff does not match.\n"
STARTED = base.STARTED
ROUTE = {"provider": "codex", "model": "gpt-6-astra", "effort": "high"}


def stamp(seconds):
    return time.strftime(judge_rounds.FORMAT, time.gmtime(seconds))


def limit_note(fails, pending, limit=3, lineage=LIN, tier="gate"):
    return ("no verdict: round limit (%d FAIL, %d pending, limit %d); the owner allows one more with JUDGE round %s %s"
            % (fails, pending, limit, lineage, tier))


class Crash(BaseException):
    """A stop of the helper that no `except Exception` and no JudgeError handler catches."""


class Rounds(base.JudgeCase):
    """The helper in this process: fake provider, fixed clock, ledger and session tools."""

    def setUp(self):
        super().setUp()
        self.t = T0
        self.answer, self.error, self.during, self.ran = PASS, None, None, []
        self.sfile, self.model = False, "gpt-6-astra"
        patcher = mock.patch.object(judge_rounds, "now", lambda: stamp(self.t))
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---- the fake provider and the run
    def fake(self, command, prompt, repo, env, seconds):
        self.ran.append(seconds)
        if self.during:
            self.during()
        if self.error:
            raise outside_judge.JudgeError(self.error)
        Path(command[command.index("-o") + 1]).write_text(self.answer)
        return ""

    def go(self, answer=PASS, tier="gate", sid=None, repo=None, lineage=None, host="claude", env=None, patch=(),
            error=None, during=None):
        self.answer, self.error, self.during, self.ran = answer, error, during, []
        row = dict(env or {})
        if sid is not None:
            row["CLAUDE_CODE_SESSION_ID"] = sid
        argv = ["--host", host, "--tier", tier] + (["--lineage", lineage] if lineage else [])
        argv += [str(self.contract), str(self.handoff), str(repo or self.repo)]
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, self.base_env(row), clear=True))
            stack.enter_context(mock.patch.object(outside_judge, "run_process", self.fake))
            for target, name, value in patch:
                stack.enter_context(mock.patch.object(target, name, value))
            out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = outside_judge.main(argv)
        lines = out.getvalue().split("\n")
        parts = lines[0].split(" | ", 3)
        return types.SimpleNamespace(code=code, out=out.getvalue(), lines=lines, ran=len(self.ran), parts=parts,
                                     word=parts[0].split()[1] if parts[0].startswith("JUDGE") else None,
                                     note=parts[3] if len(parts) == 4 else None)

    def completed(self, verdict, status, attempt=1):
        """D7: the completed record holds the verdict that the helper printed and its exit status."""
        found = [r for r in self.records("completed") if r["call"]["round"]["attempt"] == attempt]
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["judge"]["verdict"], found[0]["exit_status"]), (verdict, status))

    def first(self, verdict, note):
        return "JUDGE      %s | codex:%s | %s | %s" % (verdict, self.model, self.where(), note)

    def refused(self, result, note):
        self.assertEqual((result.code, result.ran), (3, 0), result.out)
        self.assertEqual(result.lines[0], self.first("none", note))

    def ok(self, result, verdict):
        self.assertEqual((result.code, result.ran), (0, 1), result.out)
        self.assertEqual(result.lines[0], self.first(verdict, STARTED))

    # ---- chains, ledgers, expectations
    def write_settings(self, obj):
        super().write_settings(obj)
        self.sfile = True

    def chain(self, lineage=LIN, tier="gate", repo=None):
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            project = readback_settings.resolve(repo or self.repo, "claude")
            return project, judge_rounds.paths(project["home"], project["repository"], lineage, tier)

    def path(self, lineage=LIN, tier="gate", repo=None):
        return Path(self.chain(lineage, tier, repo)[1][1])

    def digest(self):
        return readback_settings.load(str(self.project_home))["digest"]

    def ledger(self, lineage=LIN, tier="gate", repo=None):
        path = self.path(lineage, tier, repo)
        return json.loads(path.read_text()) if path.exists() else None

    def snap(self, lineage=LIN, tier="gate", repo=None):
        ledger = self.ledger(lineage, tier, repo)
        for number, item in enumerate(ledger["attempts"], 1):
            item["attempt_id"] = "#%d" % number
        return ledger

    def at(self, offset=0):
        return stamp(T0 + offset)

    def att(self, result, by="helper", at=0, finished=None, auth=None, digest=None, deadline=900, source=None):
        route = dict(ROUTE, source=source or ("settings" if self.sfile else "defaults"))
        pending = result is None
        return {"attempt_id": "", "reserved_at": self.at(at), "deadline_seconds": deadline,
                "settings_digest": digest or self.digest(), "route": route,
                "authorization": None if auth is None else {"event_id": auth, "at": self.at(0)},
                "result": result, "finished_at": None if pending else self.at(at if finished is None else finished),
                "ended_by": None if pending else by}

    def expect(self, *attempts, pin=None, tier="gate", lineage=LIN):
        project = self.chain(lineage, tier)[0]
        items = []
        for number, item in enumerate(attempts, 1):
            items.append(dict(item, attempt_id="#%d" % number))
        return {"schema_version": 1, "project_home": project["home"], "repository": project["repository"],
                "lineage": lineage, "tier": tier, "settings_digest": pin or self.digest(), "attempts": items}

    def seed(self, *attempts, pin=None, tier="gate", lineage=LIN):
        ledger = self.expect(*attempts, pin=pin, tier=tier, lineage=lineage)
        folder, path, _lock = self.chain(lineage, tier)[1]
        os.makedirs(folder, 0o700, exist_ok=True)
        judge_rounds.publish(folder, path, ledger)

    def fails(self, count):
        self.seed(*[self.att("FAIL") for _ in range(count)])

    def authorize(self, sid="sess-1", event="auth-1", lineage=LIN, tier="gate", digest=None, repository=None):
        project = self.chain(lineage, tier)[0]
        readback_session.authorize("claude", sid, event, self.at(0), project["home"], repository or project["repository"],
                                   lineage, tier, digest or self.digest(), base=str(self.sessions))

    def session(self, sid="sess-1"):
        return readback_session.read("claude", sid, base=str(self.sessions))

    def session_state(self, authorizations, events, sid="sess-1"):
        return ("valid", {"schema_version": 1, "host": "claude", "session_id": sid, "mode": None, "mode_event": None,
                          "authorizations": authorizations, "processed_events": events})

    def grant(self, event, lineage=LIN, tier="gate", digest=None, repository=None):
        project = self.chain(lineage, tier)[0]
        return {"event_id": event, "at": self.at(0), "project_home": project["home"],
                "repository": repository or project["repository"], "lineage": lineage, "tier": tier,
                "settings_digest": digest or self.digest()}

    def rounds_dir(self):
        return self.home / "logbook" / ".readback" / "rounds"

    def room(self):
        """The files of the rounds folder: ledgers, locks and any temporary file. A folder of kept texts is not listed."""
        folder = self.rounds_dir()
        return sorted(item.name for item in folder.iterdir() if not item.is_dir()) if folder.exists() else None


# ---------------------------------------------------------------------------------------------- LedgerTests

class LedgerTests(Rounds):
    def test_q01_a_pass_makes_one_attempt_with_no_fail(self):
        result = self.go()
        self.ok(result, "PASS")
        self.assertEqual(self.snap(), self.expect(self.att("PASS")))

    def test_q02_three_fail_then_a_fourth_call_is_refused_and_the_ledger_stays(self):
        for _ in range(3):
            self.ok(self.go(FAIL), "FAIL")
        self.assertEqual(self.snap(), self.expect(*[self.att("FAIL")] * 3))
        before = self.path().read_bytes()
        self.refused(self.go(PASS), limit_note(3, 0))
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(self.room(), sorted(self.path().name[:-5] + ext for ext in (".json", ".lock")))

    def test_q03_a_limit_of_one_refuses_the_second_call(self):
        self.write_settings({"judge": {"mode": "gate", "max_fail_rounds": 1}})
        self.ok(self.go(FAIL), "FAIL")
        before = self.path().read_bytes()
        self.refused(self.go(PASS), limit_note(1, 0, limit=1))
        self.assertEqual(self.path().read_bytes(), before)

    def test_q04_a_provider_error_does_not_raise_the_fail_count(self):
        self.ok(self.go(FAIL), "FAIL")
        result = self.go(error="the judge command ended with code 1")
        self.assertEqual((result.code, result.ran), (3, 1))
        self.assertEqual(result.lines[0], self.first("none", "no verdict: the judge command ended with code 1"))
        self.ok(self.go(FAIL), "FAIL")
        self.ok(self.go(FAIL), "FAIL")
        self.assertEqual(self.snap(), self.expect(self.att("FAIL"), self.att("none"), self.att("FAIL"), self.att("FAIL")))
        self.refused(self.go(PASS), limit_note(3, 0))

    def test_q05_a_pass_does_not_lower_the_fail_count(self):
        seen = []
        for answer in (FAIL, PASS, FAIL, FAIL):
            self.ok(self.go(answer), answer[:4])
            seen.append(self.att(answer[:4]))
            self.assertEqual(self.snap(), self.expect(*seen))
        before = self.path().read_bytes()
        self.refused(self.go(PASS), limit_note(3, 0))
        self.assertEqual(self.path().read_bytes(), before)

    def test_q06_another_tier_has_another_ledger(self):
        self.write_settings(base.ROUTES_FILE)
        self.model = "gpt-6.2-astra"
        gate = dict(self.att("FAIL"), route=dict(ROUTE, model="gpt-6.2-astra", effort="xhigh", source="settings"))
        for number in range(1, 4):
            self.ok(self.go(FAIL, tier="gate"), "FAIL")
            self.assertEqual(self.snap(tier="gate"), self.expect(*[gate] * number))
        before = self.path().read_bytes()
        self.refused(self.go(PASS, tier="gate"), limit_note(3, 0))
        self.assertEqual(self.path().read_bytes(), before)
        self.model = "gpt-6.2-sol"
        self.ok(self.go(PASS, tier="step"), "PASS")
        self.assertEqual(self.path().read_bytes(), before)
        step = dict(self.att("PASS", deadline=300), route=dict(ROUTE, model="gpt-6.2-sol", effort="medium",
                                                                source="settings"))
        self.assertEqual(self.snap(tier="step"), self.expect(step, tier="step"))
        self.assertEqual(len(self.room()), 4)

    def test_q07_another_contract_id_has_another_ledger(self):
        for _ in range(3):
            self.ok(self.go(FAIL), "FAIL")
        self.assertEqual(self.snap(), self.expect(*[self.att("FAIL")] * 3))
        self.put(base.CONTRACT_TRUE.replace("CON-20261117-01", "CON-20261117-02"), base.HANDOFF_TRUE)
        self.ok(self.go(PASS), "PASS")
        self.assertEqual(self.snap("CON-20261117-01"), self.expect(*[self.att("FAIL")] * 3))
        self.assertEqual(self.snap("CON-20261117-02"), self.expect(self.att("PASS"), lineage="CON-20261117-02"))

    def test_q08_an_explicit_lineage_names_the_chain_of_another_contract(self):
        self.put(base.CONTRACT_TRUE.replace("CON-20261117-01", "CON-20261117-02"), base.HANDOFF_TRUE)
        for _ in range(3):
            self.ok(self.go(FAIL), "FAIL")
        self.put(base.CONTRACT_TRUE, base.HANDOFF_TRUE)
        other = "CON-20261117-02"
        self.assertEqual(self.snap(other), self.expect(*[self.att("FAIL")] * 3, lineage=other))
        before = self.path(other).read_bytes()
        self.refused(self.go(PASS, lineage=other), limit_note(3, 0, lineage=other))
        self.assertEqual(self.path(other).read_bytes(), before)
        self.assertIsNone(self.ledger())
        self.ok(self.go(PASS), "PASS")  # the chain of the contract's own id is empty
        self.assertEqual(self.snap(), self.expect(self.att("PASS")))

    def test_q09_a_contract_without_an_id_gives_the_note_and_no_ledger(self):
        self.put("Do the thing without an id.\nACC-1  The tool shall work.   Check: sh a.sh\n", base.HANDOFF_TRUE)
        valid = {"valid": True, "findings": []}
        row = [(outside_judge.review_receipt, "run_validation", lambda *a: valid)]
        result = self.go(patch=row)
        self.refused(result, "no verdict: the contract has no CON id; pass --lineage")
        self.assertIsNone(self.room())
        self.ok(self.go(patch=row, lineage="CON-20261117-03"), "PASS")  # the explicit lineage names the chain
        self.assertEqual(len(self.snap("CON-20261117-03")["attempts"]), 1)

    def test_q10_off_and_skipped_touch_no_ledger_and_no_folder(self):
        self.write_settings({"judge": {"mode": "off"}})
        result = self.go()
        self.assertEqual((result.code, result.word, result.ran), (0, "off", 0))
        self.assertEqual(result.note, base.off_note(base.TEXT_PF))
        self.write_settings({"judge": {"mode": "gate"}})
        result = self.go(tier="step")
        self.assertEqual((result.code, result.word, result.ran), (0, "skipped", 0))
        self.assertEqual(result.note, base.skipped_note(base.TEXT_PF))
        self.assertIsNone(self.room())
        self.assertFalse((self.home / "logbook" / ".readback" / "rounds").exists())

    def test_q11_a_pending_attempt_past_its_time_ends_as_none_by_recovery(self):
        self.seed(self.att(None, at=-960))  # reserved_at + 900 + 60 is exactly now
        self.ok(self.go(), "PASS")
        self.assertEqual(self.snap(), self.expect(self.att("none", by="recovery", at=-960, finished=0), self.att("PASS")))

    def test_q11_one_second_before_the_time_the_attempt_stays_pending(self):
        self.seed(self.att(None, at=-959))
        self.ok(self.go(), "PASS")
        self.assertEqual(self.snap(), self.expect(self.att(None, at=-959), self.att("PASS")))

    def test_q12_two_pending_attempts_within_their_time_and_one_fail_refuse_the_call(self):
        self.seed(self.att("FAIL"), self.att(None, at=-100), self.att(None, at=-10))
        before = self.path().read_bytes()
        self.refused(self.go(), limit_note(1, 2))
        self.assertEqual(self.path().read_bytes(), before)

    def test_q12_the_last_free_attempt_is_the_one_below_the_limit(self):
        self.seed(self.att("FAIL"), self.att(None, at=-10))  # F + P is 2, below 3
        self.ok(self.go(), "PASS")
        self.assertEqual(len(self.snap()["attempts"]), 3)

    def test_f01_a_clock_that_moves_31_seconds_before_the_launch_expires_the_attempt(self):
        def moved(seconds):
            real = outside_judge.start_receipt

            def wrapped(*args, **kwargs):
                answer = real(*args, **kwargs)
                self.t += seconds
                return answer
            return [(outside_judge, "start_receipt", wrapped)]
        result = self.go(patch=moved(31))
        self.refused(result, "no verdict: the reservation expired")
        self.completed("none", 3)
        self.assertEqual(self.snap(), self.expect(self.att("none", finished=31)))
        self.t = T0
        shutil.rmtree(self.rounds_dir())
        result = self.go(patch=moved(30))  # exactly 30 seconds still launch
        self.ok(result, "PASS")
        self.assertEqual(self.snap(), self.expect(self.att("PASS", finished=30)))

    def test_f02_an_attempt_ended_by_recovery_while_the_provider_runs_gives_none(self):
        def recover_it():
            ledger = self.ledger()
            ledger["attempts"][0].update(result="none", finished_at=self.at(0), ended_by="recovery")
            judge_rounds.publish(str(self.rounds_dir()), str(self.path()), ledger)
        result = self.go(PASS, during=recover_it)
        self.assertEqual((result.code, result.ran), (3, 1))
        self.assertEqual(result.lines[0], self.first("none", "no verdict: the reservation expired"))
        self.assertEqual(self.snap(), self.expect(self.att("none", by="recovery")))
        self.completed("none", 3)

    def test_f03_f04_a_failing_terminal_publication_gives_none_and_leaves_the_attempt_pending(self):
        calls = []
        real = judge_rounds._rename

        def second_fails(source, target):
            calls.append(target)
            if len(calls) == 2:
                raise OSError("injected")
            real(source, target)
        seen = []
        original = outside_judge.print_result

        def spy(*args, **kwargs):
            seen.append(args[0])
            return original(*args, **kwargs)
        result = self.go(FAIL, patch=[(judge_rounds, "_rename", second_fails), (outside_judge, "print_result", spy)])
        self.assertEqual((result.code, result.ran, seen), (3, 1, ["none"]))
        self.assertEqual(result.lines[0], self.first("none", "no verdict: the round accounting failed"))
        self.assertEqual(self.snap(), self.expect(self.att(None)))
        self.completed("none", 3)
        self.assertEqual(len(self.room()), 2)  # the ledger and the lock: no temporary file
        # F4: after the time of D3 step 2 the attempt ends as none and F stays 0
        self.t = T0 + 960
        self.ok(self.go(PASS), "PASS")
        self.assertEqual(self.snap(), self.expect(self.att("none", by="recovery", finished=960),
                                                  self.att("PASS", at=960)))
        started = [r for r in self.records("started") if r["call"]["round"]["attempt"] == 2]
        self.assertEqual(started[0]["call"]["round"]["fails_before"], 0)

    def test_f05_a_provider_error_ends_the_attempt_as_none_by_the_helper(self):
        result = self.go(error="the judge exceeded the deadline")
        self.assertEqual((result.code, result.ran), (3, 1))
        self.assertEqual(result.lines[0], self.first("none", "no verdict: the judge exceeded the deadline"))
        self.assertEqual(self.snap(), self.expect(self.att("none")))
        self.completed("none", 3)

    def test_f05_a_failing_publication_after_a_provider_error_gives_the_accounting_note(self):
        calls = []
        real = judge_rounds._rename

        def second_fails(source, target):
            calls.append(target)
            if len(calls) == 2:
                raise OSError("injected")
            real(source, target)
        result = self.go(error="the judge exceeded the deadline", patch=[(judge_rounds, "_rename", second_fails)])
        self.assertEqual(result.lines[0], self.first("none", "no verdict: the round accounting failed"))
        self.assertEqual(self.snap(), self.expect(self.att(None)))

    def test_f07_an_interrupted_provider_call_ends_the_attempt_as_none(self):
        def interrupted(*args):
            raise KeyboardInterrupt()
        result = self.go(patch=[(outside_judge, "run_process", interrupted)])
        self.assertEqual((result.code, result.word), (3, "none"))
        self.assertEqual(result.note, "no verdict: the judge was interrupted")
        self.assertEqual(self.snap(), self.expect(self.att("none")))

    def test_f08_a_ledger_that_is_gone_when_the_provider_ended_counts_as_expired(self):
        result = self.go(PASS, during=lambda: self.path().unlink())
        self.assertEqual((result.code, result.ran), (3, 1))
        self.assertEqual(result.lines[0], self.first("none", "no verdict: the reservation expired"))

    def test_f06_the_terminal_result_is_published_before_the_verdict_is_printed(self):
        for answer, word in ((PASS, "PASS"), (FAIL, "FAIL")):
            with self.subTest(verdict=word):
                shutil.rmtree(self.rounds_dir(), ignore_errors=True)
                shutil.rmtree(self.common / "readback", ignore_errors=True)
                self.t = T0
                seen = []
                original = outside_judge.print_result

                def spy(*args, **kwargs):
                    ledger = self.ledger()["attempts"][0]
                    seen.append((args[0], ledger["result"], ledger["finished_at"], ledger["ended_by"]))
                    return original(*args, **kwargs)
                self.t = T0 + 5
                result = self.go(answer, patch=[(outside_judge, "print_result", spy)])
                self.ok(result, word)
                self.assertEqual(seen, [(word, word, self.at(5), "helper")])
                self.assertEqual(self.snap(), self.expect(self.att(word, at=5)))
                self.completed(word, 0)

    # ---- pure functions
    def test_u01_the_lineage_of_a_contract_is_the_id_of_its_first_line_that_is_not_blank(self):
        cases = (("CON-20261118-17  Add it.\nmore", "CON-20261118-17"), ("\n  \nCON-20261118-17 x", "CON-20261118-17"),
                 ("CON-20261118-17", "CON-20261118-17"), ("CON-20261118-170 x", None), ("CON-2026111-17 x", None),
                 ("con-20261118-17 x", None), ("Title\nCON-20261118-17 x", None), ("   \n", None),
                 ("CON-20261118-17x y", None))
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(judge_rounds.lineage_of(text), expected)

    def test_u02_the_chain_key_is_the_sha256_of_the_canonical_list(self):
        expected = hashlib.sha256(b'["/home/user/p","/home/user/r","CON-20261118-17","gate"]').hexdigest()
        self.assertEqual(judge_rounds.chain_key("/home/user/p", "/home/user/r", "CON-20261118-17", "gate"), expected)
        self.assertNotEqual(expected, judge_rounds.chain_key("/home/user/p", "/home/user/r", "CON-20261118-17", "step"))
        folder, ledger, lock = judge_rounds.paths("/home/user/p", "/home/user/r", "CON-20261118-17", "gate")
        with mock.patch.dict(os.environ, {"HOME": "/home/user"}):
            self.assertEqual(judge_rounds.paths("p", "r", "l", "gate")[0], "/home/user/logbook/.readback/rounds")
        self.assertEqual((ledger, lock), (os.path.join(folder, expected + ".json"), os.path.join(folder, expected + ".lock")))

    def test_u03_the_clock_gives_utc_seconds_and_parses_back(self):
        self.assertEqual(stamp(T0), "2026-11-18T10:00:00Z")
        self.assertEqual(judge_rounds.seconds_of("2026-11-18T10:00:00Z"), T0)
        self.assertRegex(time.strftime(judge_rounds.FORMAT, time.gmtime()), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_u04_recovery_counts_and_the_decision(self):
        def ledger(*attempts, pin="d"):
            return {"settings_digest": pin, "attempts": [dict(a, attempt_id=str(n), reserved_at=self.at(a.pop("at", 0)))
                                                         for n, a in enumerate(attempts)]}

        def item(result, at=0, deadline=900):
            return {"result": result, "at": at, "deadline_seconds": deadline, "finished_at": None, "ended_by": None}
        pending = ledger(item(None, -960), item(None, -959), item(None, -61, deadline=0), item(None, -60, deadline=0),
                         item("FAIL"), item("PASS"))
        judge_rounds.recover(pending, self.at(0))
        self.assertEqual([a["result"] for a in pending["attempts"]], ["none", None, "none", "none", "FAIL", "PASS"])
        self.assertEqual([a["ended_by"] for a in pending["attempts"]], ["recovery", None, "recovery", "recovery", None, None])
        self.assertEqual([a["finished_at"] for a in pending["attempts"]][:3], [self.at(0), None, self.at(0)])
        self.assertEqual(judge_rounds.counts(pending), (1, 1))
        calls = []

        def take():
            calls.append(1)
            return {"event_id": "e"}
        free = ledger(item("FAIL"), item("FAIL"))
        self.assertEqual(judge_rounds.decide(free, "d", 3, LIN, "gate", take), (2, None))
        self.assertEqual(calls, [])
        full = ledger(item("FAIL"), item("FAIL"), item(None))
        self.assertEqual(judge_rounds.decide(full, "d", 3, LIN, "gate", take), (2, {"event_id": "e"}))
        self.assertEqual(len(calls), 1)
        both = ledger(item("FAIL"), item("FAIL"), item("FAIL"), pin="old")
        self.assertEqual(judge_rounds.decide(both, "d", 3, LIN, "gate", take), (3, {"event_id": "e"}))
        self.assertEqual((len(calls), both["settings_digest"]), (2, "d"))  # one authorization serves D4 and D5
        with self.assertRaises(judge_rounds.RoundError) as raised:
            judge_rounds.decide(ledger(item("FAIL"), pin="old"), "d", 3, LIN, "gate", lambda: None)
        self.assertEqual(raised.exception.note, "settings changed in the chain")
        with self.assertRaises(judge_rounds.RoundError) as raised:
            judge_rounds.decide(ledger(item("FAIL"), pin="old"), "d", 1, LIN, "gate", lambda: None)
        self.assertEqual(raised.exception.note, "settings changed in the chain")  # the pin comes before the limit
        with self.assertRaises(judge_rounds.RoundError) as raised:
            judge_rounds.decide(ledger(item("FAIL")), "d", 1, LIN, "step", lambda: None)
        self.assertEqual(raised.exception.note, limit_note(1, 0, limit=1, tier="step")[len("no verdict: "):])

    def test_u06_confirm_ends_a_late_attempt_itself_and_finish_changes_only_a_pending_one(self):
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))
        project = self.chain()[0]
        args = ("claude", None, project["home"], project["repository"], LIN, "gate", "d", 3, {"provider": "codex"}, 900)
        for late, seconds in ((False, 30), (True, 31)):
            with self.subTest(seconds=seconds):
                shutil.rmtree(self.rounds_dir(), ignore_errors=True)
                self.t = T0
                held = judge_rounds.reserve(*args)
                self.assertEqual(held.record, {"attempt": 1, "fails_before": 0, "authorization": None})
                self.t = T0 + seconds
                if late:
                    with self.assertRaises(judge_rounds.RoundError) as raised:
                        judge_rounds.confirm(held)
                    self.assertEqual(raised.exception.note, "the reservation expired")
                    attempt = self.ledger()["attempts"][0]
                    self.assertEqual((attempt["result"], attempt["finished_at"], attempt["ended_by"]),
                                     ("none", self.at(31), "helper"))
                    with self.assertRaises(judge_rounds.RoundError):  # a result that is set stays
                        judge_rounds.finish(held, "PASS")
                    self.assertEqual(self.ledger()["attempts"][0]["result"], "none")
                else:
                    judge_rounds.confirm(held)
                    self.assertIsNone(self.ledger()["attempts"][0]["result"])
                    judge_rounds.finish(held, "FAIL")
                    self.assertEqual(self.ledger()["attempts"][0]["result"], "FAIL")
                    with self.assertRaises(judge_rounds.RoundError):
                        judge_rounds.finish(held, "PASS")
                    self.assertEqual(self.ledger()["attempts"][0]["result"], "FAIL")

    def test_u05_the_shape_of_a_ledger(self):
        identity = {"project_home": "/home/user/p", "repository": "/home/user/r", "lineage": LIN, "tier": "gate"}
        good = {"attempt_id": "a", "reserved_at": "2026-11-18T10:00:00Z", "deadline_seconds": 900,
                "settings_digest": "d", "route": {"provider": "codex"}, "authorization": None, "result": None,
                "finished_at": None, "ended_by": None}
        grant = {"event_id": "e", "at": "2026-11-18T10:00:00Z"}
        ended = dict(good, result="FAIL", finished_at="2026-11-18T10:00:01Z", ended_by="recovery")

        def ledger(*attempts, **change):
            made = dict(identity, schema_version=1, settings_digest="d", attempts=list(attempts))
            made.update(change)
            return made
        fine = [ledger(), ledger(good), ledger(ended), ledger(dict(good, deadline_seconds=7.5)),
                ledger(dict(good, deadline_seconds=0.5)), ledger(dict(good, deadline_seconds=1)),
                ledger(dict(ended, result="none", ended_by="helper", authorization=grant)), ledger(ended, good)]
        for number, made in enumerate(fine):
            with self.subTest(fine=number):
                self.assertFalse(judge_rounds.ledger_problem(made, identity))
        broken = {"not an object": [], "missing key": {k: v for k, v in ledger().items() if k != "tier"},
                  "unknown key": dict(ledger(), extra=1), "schema 2": ledger(schema_version=2),
                  "schema true": ledger(schema_version=True), "lineage": ledger(lineage="CON-20261117-02"),
                  "tier": ledger(tier="step"), "home": ledger(project_home="/home/user/x"),
                  "repository": ledger(repository="/home/user/x"), "pin type": ledger(settings_digest=5),
                  "attempts type": ledger(attempts={}), "attempt not an object": ledger([]),
                  "attempt extra key": ledger(dict(good, more=1)),
                  "attempt missing key": ledger({k: v for k, v in good.items() if k != "route"}),
                  "deadline zero": ledger(dict(good, deadline_seconds=0)),
                  "deadline negative": ledger(dict(good, deadline_seconds=-1)),
                  "deadline text": ledger(dict(good, deadline_seconds="9")),
                  "deadline bool": ledger(dict(good, deadline_seconds=True)),
                  "id empty": ledger(dict(good, attempt_id="")), "id type": ledger(dict(good, attempt_id=1)),
                  "digest empty": ledger(dict(good, settings_digest="")),
                  "reserved form": ledger(dict(good, reserved_at="2026-11-18 10:00:00")),
                  "reserved impossible": ledger(dict(good, reserved_at="2026-13-45T10:00:00Z")),
                  "reserved type": ledger(dict(good, reserved_at=5)),
                  "route type": ledger(dict(good, route="codex")),
                  "authorization type": ledger(dict(good, authorization="e")),
                  "authorization key": ledger(dict(good, authorization={"event_id": "e"})),
                  "authorization extra": ledger(dict(good, authorization=dict(grant, x=1))),
                  "authorization id": ledger(dict(good, authorization=dict(grant, event_id=3))),
                  "authorization at": ledger(dict(good, authorization=dict(grant, at="now"))),
                  "result unknown": ledger(dict(ended, result="PENDING")),
                  "result without finish": ledger(dict(ended, finished_at=None)),
                  "result without end": ledger(dict(ended, ended_by=None)),
                  "finish without result": ledger(dict(good, finished_at="2026-11-18T10:00:01Z")),
                  "end without result": ledger(dict(good, ended_by="helper")),
                  "ended_by unknown": ledger(dict(ended, ended_by="owner")),
                  "finished form": ledger(dict(ended, finished_at="soon"))}
        for name, made in broken.items():
            with self.subTest(broken=name):
                self.assertTrue(judge_rounds.ledger_problem(made, identity))


# ------------------------------------------------------------------------------------- AuthorizationTests

class AuthorizationTests(Rounds):
    def test_a01_a02_one_authorization_permits_the_fourth_attempt(self):
        self.fails(3)
        self.authorize()
        self.ok(self.go(PASS, sid="sess-1"), "PASS")
        self.assertEqual(self.session(), self.session_state([], ["auth-1"]))
        self.assertEqual(self.snap(), self.expect(*[self.att("FAIL")] * 3, self.att("PASS", auth="auth-1")))
        # A2: one authorization permits one attempt
        before = self.path().read_bytes()
        self.refused(self.go(PASS, sid="sess-1"), limit_note(3, 0))
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(self.session(), self.session_state([], ["auth-1"]))

    def test_a03_an_authorization_for_another_target_is_refused_and_stays(self):
        self.fails(3)
        rows = (("tier", dict(tier="step")), ("lineage", dict(lineage="CON-20261117-09")),
                ("repository", dict(repository="/home/user/other")), ("digest", dict(digest="f" * 64)))
        for number, (name, change) in enumerate(rows):
            with self.subTest(target=name):
                sid = "sess-%d" % number
                self.authorize(sid=sid, **change)
                before = self.session(sid)
                self.refused(self.go(PASS, sid=sid), limit_note(3, 0))
                self.assertEqual(self.session(sid), before)
                self.assertEqual(len(before[1]["authorizations"]), 1)
        self.assertEqual(self.snap(), self.expect(*[self.att("FAIL")] * 3))

    def test_a04_the_earliest_appended_authorization_is_consumed_first(self):
        self.fails(3)
        self.authorize(event="e1")
        self.authorize(event="e2")
        self.ok(self.go(FAIL, sid="sess-1"), "FAIL")
        self.assertEqual(self.session(), self.session_state([self.grant("e2")], ["e1", "e2"]))
        self.ok(self.go(FAIL, sid="sess-1"), "FAIL")
        self.assertEqual(self.session(), self.session_state([], ["e1", "e2"]))
        self.refused(self.go(PASS, sid="sess-1"), limit_note(5, 0))
        self.assertEqual(self.snap(), self.expect(*[self.att("FAIL")] * 3, self.att("FAIL", auth="e1"),
                                                  self.att("FAIL", auth="e2")))

    def settings_change(self):
        self.write_settings({"judge": {"mode": "gate", "deadline_seconds": {"step": 300, "gate": 800}}})

    def test_a05_a_settings_change_without_an_authorization_is_refused(self):
        self.ok(self.go(FAIL), "FAIL")
        old = self.digest()
        self.settings_change()
        before = self.path().read_bytes()
        self.refused(self.go(PASS), "no verdict: settings changed in the chain")
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(self.snap(), self.expect(self.att("FAIL", digest=old, source="defaults"), pin=old))

    def test_a06_an_authorization_for_the_new_digest_moves_the_pin(self):
        self.ok(self.go(FAIL), "FAIL")
        old = self.digest()
        self.settings_change()
        self.authorize()
        self.ok(self.go(PASS, sid="sess-1"), "PASS")
        self.assertEqual(self.session(), self.session_state([], ["auth-1"]))
        self.assertEqual(self.snap(), self.expect(
            self.att("FAIL", digest=old, source="defaults"), self.att("PASS", auth="auth-1", deadline=800,
                                                                       source="settings"), pin=self.digest()))
        self.assertNotEqual(old, self.digest())

    def test_a07_one_authorization_serves_the_limit_and_the_pin_of_one_reservation(self):
        for _ in range(3):
            self.go(FAIL)
        old = self.digest()
        self.settings_change()
        self.authorize()
        self.ok(self.go(PASS, sid="sess-1"), "PASS")
        self.assertEqual(self.session(), self.session_state([], ["auth-1"]))
        new = self.digest()
        self.assertEqual(self.snap(), self.expect(*[self.att("FAIL", digest=old, source="defaults")] * 3,
                                                  self.att("PASS", auth="auth-1", deadline=800, source="settings"), pin=new))
        before = self.path().read_bytes()
        self.refused(self.go(PASS, sid="sess-1"), limit_note(3, 0))
        self.assertEqual(self.path().read_bytes(), before)

    def test_a08_an_authorization_for_the_old_digest_does_not_serve_the_new_one(self):
        for _ in range(3):
            self.go(FAIL)
        old = self.digest()
        self.settings_change()
        self.authorize(digest=old)
        before = self.path().read_bytes()
        self.refused(self.go(PASS, sid="sess-1"), "no verdict: settings changed in the chain")
        self.assertEqual(self.session(), self.session_state([self.grant("auth-1", digest=old)], ["auth-1"]))
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(self.snap(), self.expect(*[self.att("FAIL", digest=old, source="defaults")] * 3, pin=old))

    def test_a09_without_a_session_id_three_fail_are_refused(self):
        self.fails(3)
        self.authorize()
        before = self.path().read_bytes()
        self.refused(self.go(PASS), limit_note(3, 0))
        self.assertEqual(self.session(), self.session_state([self.grant("auth-1")], ["auth-1"]))
        self.assertEqual(self.path().read_bytes(), before)

    def test_a10_a_free_attempt_leaves_a_matching_authorization_in_the_session(self):
        self.fails(1)
        self.authorize()
        self.ok(self.go(PASS, sid="sess-1"), "PASS")
        self.assertEqual(self.session(), self.session_state([self.grant("auth-1")], ["auth-1"]))
        self.assertEqual(self.snap(), self.expect(self.att("FAIL"), self.att("PASS")))

    def test_a11_a_stop_between_the_consumption_and_the_publication_loses_the_authorization(self):
        self.fails(3)
        self.authorize()
        before = self.path().read_bytes()

        def crash(source, target):
            raise Crash()
        with self.assertRaises(Crash):
            self.go(PASS, sid="sess-1", patch=[(judge_rounds, "_rename", crash)])
        self.assertEqual(self.ran, [])
        self.assertEqual(self.session(), self.session_state([], ["auth-1"]))
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(self.room(), sorted(self.path().name[:-5] + ext for ext in (".json", ".lock")))
        self.refused(self.go(PASS, sid="sess-1"), limit_note(3, 0))

    def test_a12_consume_is_called_while_the_ledger_lock_is_held(self):
        self.fails(3)
        self.authorize()
        _project, (folder, _ledger, lock) = self.chain()
        session_lock = self.sessions / "claude-sess-1.lock"
        held = os.open(session_lock, os.O_RDWR | os.O_CREAT, 0o600)
        probe = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, held)
        self.addCleanup(os.close, probe)
        fcntl.flock(held, fcntl.LOCK_EX)
        process = subprocess.Popen([sys.executable, "-B", str(HELPER), "--host", "claude", "--tier", "gate",
                                    str(self.contract), str(self.handoff), str(self.repo)],
                                   env=self.base_env({"CLAUDE_CODE_SESSION_ID": "sess-1"}),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))

        def blocked():
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(probe, fcntl.LOCK_UN)
            return False
        deadline = time.time() + 40
        while not blocked() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(blocked(), "the helper never held the ledger lock")
        time.sleep(0.5)
        self.assertIsNone(process.poll())  # waiting for the session lock
        self.assertTrue(blocked())  # and still holding the ledger lock
        fcntl.flock(held, fcntl.LOCK_UN)
        out, _err = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, out)
        self.assertTrue(out.decode().startswith("JUDGE      PASS"), out)
        self.assertEqual(self.session(), self.session_state([], ["auth-1"]))
        attempt = self.snap()["attempts"][3]  # real clock: the times are not compared
        self.assertEqual((attempt["result"], attempt["ended_by"], attempt["authorization"]["event_id"],
                          attempt["settings_digest"]), ("PASS", "helper", "auth-1", self.digest()))
        self.assertEqual(self.snap()["settings_digest"], self.digest())


# -------------------------------------------------------------------------------------------- RaceTests

SLOW_CLI = r'''
import json, os, pathlib, subprocess, sys, time
args = sys.argv[1:]
prompt = sys.stdin.buffer.read()
with open(os.environ["FAKE_RECORD"], "a") as record:
    record.write(json.dumps({"provider": "codex", "pid": os.getpid()}) + "\n")
if os.environ.get("FAKE_PIDS"):
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL} if os.environ.get("FAKE_DESC") else {}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], **quiet)
    pathlib.Path(os.environ["FAKE_PIDS"]).write_text("%d %d %r" % (os.getpid(), child.pid, time.time()))
go = os.environ.get("FAKE_GO")
while go and not os.path.exists(go):
    time.sleep(0.05)
if os.environ.get("FAKE_HANG"):
    time.sleep(600)
pathlib.Path(args[args.index("-o") + 1]).write_text(os.environ.get("FAKE_ANSWER", "PASS ok\n"))
'''

DRIVER = '''
import os, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import judge_rounds, outside_judge
def stall(reservation):
    open(os.environ["DRIVER_MARK"], "w").write("reserved")
    time.sleep(600)
judge_rounds.confirm = stall
sys.exit(outside_judge.main(sys.argv[2:]))
'''


DRIVER_KILL = '''
import os, signal, subprocess, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import outside_judge
active, count, real, run = [False], [0], subprocess.Popen, outside_judge.run_process
class Spy(real):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if active[0]:
            with open(os.environ["DRIVER_PIDS"], "a") as handle:
                handle.write("%d\\n" % self.pid)
            count[0] += 1
            if count[0] == int(os.environ["KILL_AFTER"]):
                os.kill(os.getpid(), signal.SIGKILL)
def wrapped(*args):
    active[0] = True
    return run(*args)
subprocess.Popen = Spy
outside_judge.run_process = wrapped
sys.exit(outside_judge.main(sys.argv[2:]))
'''


class RaceTests(Rounds):
    def setUp(self):
        super().setUp()
        fake = self.base / "bin" / "codex"
        fake.write_text("#!" + sys.executable + "\n" + SLOW_CLI)
        fake.chmod(0o755)
        self.procs, self.pids = [], []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for process in self.procs:
            if process.poll() is None:
                process.kill()
            process.wait()
        for pid in self.pids:
            for send in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
                with contextlib.suppress(OSError):
                    send()

    def spawn(self, env=None, argv=None):
        env = self.base_env(env)
        argv = argv or [sys.executable, "-B", str(HELPER), "--host", "claude", "--tier", "gate",
                        str(self.contract), str(self.handoff), str(self.repo)]
        process = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(process)
        return process

    def wait_for(self, condition, seconds=60):
        deadline = time.time() + seconds
        while not condition() and time.time() < deadline:
            time.sleep(0.05)
        return condition()

    def provider_calls(self):
        return len(self.record.read_text().splitlines()) if self.record.exists() else 0

    def eight(self, env):
        go = self.base / "go"
        env = dict(env, FAKE_GO=str(go))
        group = [self.spawn(env) for _ in range(8)]
        self.wait_for(lambda: sum(p.poll() is not None for p in group) >= 7)
        go.write_text("go")
        outs = [(p.communicate(timeout=90)[0].decode(), p.returncode) for p in group]
        return outs

    def check_eight(self, outs, fails, pending_note, winner):
        refusals = [o for o, code in outs if o.startswith("JUDGE      none")]
        won = [o for o, code in outs if not o.startswith("JUDGE      none")]
        self.assertEqual(self.provider_calls(), 1)
        self.assertEqual(len(refusals), 7, outs)
        self.assertEqual(len(won), 1, outs)
        for out in refusals:
            self.assertEqual(out.split("\n")[0].split(" | ", 3)[3], pending_note)
        self.assertTrue(won[0].startswith("JUDGE      " + winner), won[0])
        self.assertEqual(sorted(code for _o, code in outs), [0] + [3] * 7)

    def test_x01_two_fail_and_eight_helpers_run_one_provider(self):
        self.fails(2)
        outs = self.eight({"FAKE_ANSWER": FAIL})
        self.check_eight(outs, 2, limit_note(2, 1), "FAIL")
        self.assertEqual([a["result"] for a in self.ledger()["attempts"]], ["FAIL"] * 3)
        self.assertEqual(len(self.ledger()["attempts"]), 3)

    def test_x02_three_fail_one_authorization_and_eight_helpers_run_one_provider(self):
        self.fails(3)
        self.authorize()
        outs = self.eight({"FAKE_ANSWER": PASS, "CLAUDE_CODE_SESSION_ID": "sess-1"})
        self.check_eight(outs, 3, limit_note(3, 1), "PASS")
        self.assertEqual(self.session(), self.session_state([], ["auth-1"]))
        attempts = self.ledger()["attempts"]
        self.assertEqual([a["result"] for a in attempts], ["FAIL"] * 3 + ["PASS"])
        self.assertEqual(attempts[3]["authorization"]["event_id"], "auth-1")

    def test_x03_a_helper_killed_after_its_reservation_leaves_a_pending_attempt(self):
        mark = self.base / "mark"
        scripts = str(base.SCRIPTS)
        process = self.spawn({"DRIVER_MARK": str(mark)}, [sys.executable, "-B", "-c", DRIVER, scripts, "--host", "claude",
                                                         "--tier", "gate", str(self.contract), str(self.handoff),
                                                         str(self.repo)])
        self.assertTrue(self.wait_for(mark.exists), "the helper never reached its launch")
        process.send_signal(signal.SIGKILL)
        process.wait()
        self.assertEqual(self.provider_calls(), 0)
        ledger = self.ledger()
        self.assertEqual([(a["result"], a["finished_at"], a["ended_by"]) for a in ledger["attempts"]], [(None, None, None)])
        # Q11 applies to it later
        ledger["attempts"][0]["reserved_at"] = stamp(int(time.time()) - 1000)  # real clock: the helper runs apart
        judge_rounds.publish(str(self.rounds_dir()), str(self.path()), ledger)
        done = subprocess.run([sys.executable, "-B", str(HELPER), "--host", "claude", "--tier", "gate",
                               str(self.contract), str(self.handoff), str(self.repo)], env=self.base_env(),
                              capture_output=True, timeout=90)
        self.assertEqual(done.returncode, 0, done.stdout)
        attempts = self.ledger()["attempts"]
        self.assertEqual([(a["result"], a["ended_by"]) for a in attempts], [("none", "recovery"), ("PASS", "helper")])

    def test_x04_a_helper_killed_after_the_provider_started_leaves_no_process_behind(self):
        pids = self.base / "pids"
        process = self.spawn({"FAKE_PIDS": str(pids), "FAKE_HANG": "1", "READBACK_JUDGE_SECONDS": "2"})
        self.assertTrue(self.wait_for(lambda: pids.exists() and len(pids.read_text().split()) == 3), "no provider")
        fields = pids.read_text().split()
        provider, child, began = int(fields[0]), int(fields[1]), float(fields[2])
        self.pids.extend([provider, child])
        process.send_signal(signal.SIGKILL)
        process.wait()

        def gone(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False
        self.assertTrue(self.wait_for(lambda: gone(provider) and gone(child), 7), "a process of the provider is left")
        self.assertLess(time.time() - began, 7)  # D8: within the deadline of 2 plus 5 seconds of the provider's start
        self.assertEqual([(a["result"], a["ended_by"]) for a in self.ledger()["attempts"]], [(None, None)])

    def bounded(self, command, seconds):
        """Run `run_process` for real with a recording watchdog; return (outcome, the watchdog process)."""
        seen = []
        real = judge_rounds.start_reaper

        def recording(limit):
            found = real(limit)
            seen.append(found[0])
            self.procs.append(found[0])
            return found
        with mock.patch.object(judge_rounds, "start_reaper", recording):
            try:
                outcome = outside_judge.run_process(command, "prompt", str(self.repo), dict(os.environ), seconds)
            except Exception as error:
                outcome = error
        return outcome, seen

    def test_x07_the_watchdog_ends_with_the_run_and_the_output_is_unchanged(self):
        outcome, seen = self.bounded([sys.executable, "-c", "import sys; print(sys.stdin.read() + '!')"], 10)
        self.assertEqual(outcome, "prompt!\n")
        self.assertTrue(self.wait_for(lambda: seen[0].poll() is not None, 5))  # it ends by itself: the group is gone
        self.assertEqual(len(seen), 1)

    def test_x07_a_deadline_stops_the_group_and_the_watchdog(self):
        pidfile = self.base / "pid"
        code = "import os, time; open(%r, 'w').write(str(os.getpid())); time.sleep(60)" % str(pidfile)
        outcome, seen = self.bounded([sys.executable, "-c", code], 1)
        self.pids.append(int(pidfile.read_text()))
        self.assertIsInstance(outcome, outside_judge.JudgeError)
        self.assertEqual(str(outcome), "the judge exceeded the deadline")
        self.assertTrue(self.wait_for(lambda: seen[0].poll() is not None, 5))
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_x07_a_watchdog_that_cannot_start_means_no_provider(self):
        pidfile = self.base / "pid"
        code = "import os, time; open(%r, 'w').write(str(os.getpid())); time.sleep(60)" % str(pidfile)

        def cannot(limit):
            raise OSError("no process")
        with mock.patch.object(judge_rounds, "start_reaper", cannot):
            with self.assertRaises(outside_judge.JudgeError) as raised:
                outside_judge.run_process([sys.executable, "-c", code], "", str(self.repo), dict(os.environ), 30)
        self.assertEqual(str(raised.exception), "could not start the judge command")
        time.sleep(1)
        self.assertFalse(pidfile.exists())

    def run_driver(self, kill_after, env):
        """The helper with a Popen wrapper: SIGKILL to itself right after the nth start that run_process makes."""
        pids = self.base / "driver-pids"
        row = dict(env, KILL_AFTER=str(kill_after), DRIVER_PIDS=str(pids))
        process = self.spawn(row, [sys.executable, "-B", "-c", DRIVER_KILL, str(base.SCRIPTS), "--host", "claude",
                                   "--tier", "gate", str(self.contract), str(self.handoff), str(self.repo)])
        return process, pids

    def started_pids(self, pids):
        found = [int(x) for x in pids.read_text().split()] if pids.exists() else []
        self.pids.extend(found)
        return found

    def test_x10_a_helper_killed_after_the_start_of_the_watchdog_leaves_no_provider_and_no_watchdog(self):
        process, pids = self.run_driver(1, {"FAKE_PIDS": str(self.base / "pids"), "FAKE_HANG": "1",
                                            "READBACK_JUDGE_SECONDS": "2"})
        process.wait(timeout=60)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        found = self.started_pids(pids)
        self.assertEqual(len(found), 1)  # only the watchdog
        self.assertTrue(self.wait_for(lambda: not self.alive(found[0]), 5), "the watchdog is alive")
        time.sleep(1)
        self.assertEqual(self.provider_calls(), 0)
        self.assertEqual([(a["result"], a["ended_by"]) for a in self.ledger()["attempts"]], [(None, None)])

    def test_x10_a_helper_killed_directly_after_the_start_of_the_provider_leaves_nothing_alive(self):
        pidfile = self.base / "pids"
        process, pids = self.run_driver(2, {"FAKE_PIDS": str(pidfile), "FAKE_HANG": "1", "READBACK_JUDGE_SECONDS": "2"})
        process.wait(timeout=60)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertTrue(self.wait_for(lambda: pidfile.exists() and len(pidfile.read_text().split()) == 3), "no provider")
        fields = pidfile.read_text().split()
        provider, child, began = int(fields[0]), int(fields[1]), float(fields[2])
        self.pids.extend([provider, child])
        found = self.started_pids(pids)
        self.assertEqual(len(found), 2)  # the watchdog and the provider
        self.assertTrue(self.wait_for(lambda: all(not self.alive(p) for p in [provider, child] + found), 7))
        self.assertLess(time.time() - began, 7)
        self.assertEqual([(a["result"], a["ended_by"]) for a in self.ledger()["attempts"]], [(None, None)])

    def test_x11_a_descendant_after_a_normal_exit_is_ended_at_the_bound_and_the_watchdog_ends_after_it(self):
        pidfile = self.base / "pids"
        process, pids = self.run_driver(0, {"FAKE_PIDS": str(pidfile), "FAKE_DESC": "1", "READBACK_JUDGE_SECONDS": "2"})
        out, _err = process.communicate(timeout=30)  # the helper does not wait for the child
        self.assertEqual(process.returncode, 0, out)
        self.assertTrue(out.decode().startswith("JUDGE      PASS"), out)
        fields = pidfile.read_text().split()
        provider, child, began = int(fields[0]), int(fields[1]), float(fields[2])
        self.pids.extend([provider, child])
        self.assertEqual([(a["result"], a["ended_by"]) for a in self.ledger()["attempts"]], [("PASS", "helper")])
        self.assertTrue(self.alive(child) or time.time() - began > 1)
        self.assertTrue(self.wait_for(lambda: not self.alive(child), 7))
        self.assertLess(time.time() - began, 7)
        watchdog = self.started_pids(pids)[0]
        self.assertTrue(self.wait_for(lambda: not self.alive(watchdog), 5), "the watchdog is alive")

    def test_x12_a_normal_run_leaves_no_watchdog_behind(self):
        process, pids = self.run_driver(0, {})
        out, _err = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, out)
        watchdog = self.started_pids(pids)[0]
        self.assertTrue(self.wait_for(lambda: not self.alive(watchdog), 5), "the watchdog is alive")

    def test_x09_the_watchdog_kills_the_group_only_at_its_time_and_never_a_group_below_2(self):
        for pgid in (1, 0, -4):
            with self.subTest(pgid=pgid):
                sent, clock = [], iter([5.0] * 4)
                with mock.patch.object(judge_rounds.os, "killpg", lambda *a: sent.append(a)), \
                        mock.patch.object(judge_rounds.time, "sleep", lambda seconds: None), \
                        mock.patch.object(judge_rounds.time, "time", lambda: next(clock)):
                    judge_rounds.reap(pgid, 0.0)
                    judge_rounds.reap(pgid, 9.0)
                self.assertEqual(sent, [])
        for pgid in (2, 99):
            clock = iter([0.0, 1.0, 2.0])
            sent = []
            with mock.patch.object(judge_rounds.os, "killpg", lambda *a: sent.append(a)), \
                    mock.patch.object(judge_rounds.time, "time", lambda: next(clock)), \
                    mock.patch.object(judge_rounds.time, "sleep", lambda seconds: None):
                judge_rounds.reap(pgid, 2.0)
            self.assertEqual(sent, [(pgid, 0), (pgid, 0), (pgid, signal.SIGKILL)])
        for error in (ProcessLookupError, PermissionError):
            sent = []

            def probe(pgid, sig):
                sent.append((pgid, sig))
                raise error()
            with mock.patch.object(judge_rounds.os, "killpg", probe), \
                    mock.patch.object(judge_rounds.time, "time", lambda: 0.0):
                judge_rounds.reap(99, 50.0)
            self.assertEqual(sent, [(99, 0)])

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_x05_a_rounds_folder_without_write_permission_gives_the_accounting_note(self):
        folder = self.rounds_dir()
        folder.mkdir(parents=True)
        folder.chmod(0o500)
        self.addCleanup(folder.chmod, 0o700)
        result = self.run_helper("claude", "gate")
        self.assertEqual((result.code, result.word, result.calls), (3, "none", []))
        self.assertEqual(result.note, "no verdict: the round accounting failed")
        self.assertEqual(sorted(folder.iterdir()), [])

    def test_x06_a_ledger_that_is_not_valid_gives_the_invalid_note_and_no_provider_call(self):
        self.fails(1)
        path = self.path()
        good = json.loads(path.read_text())
        bad = {"not JSON": b"{broken", "empty": b"", "other lineage": dict(good, lineage="CON-20261117-02"),
               "unknown key": dict(good, extra=1), "other tier": dict(good, tier="step"),
               "an array": [], "attempt with a unknown key": dict(good, attempts=[dict(good["attempts"][0], x=1)])}
        for name, content in bad.items():
            with self.subTest(ledger=name):
                path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
                before = path.read_bytes()
                result = self.run_helper("claude", "gate")
                self.assertEqual((result.code, result.word, result.calls), (3, "none", []))
                self.assertEqual(result.note, "no verdict: the round ledger is invalid")
                self.assertEqual(path.read_bytes(), before)


# --------------------------------------------------------------------------------------------- ChainTests

class ChainTests(Rounds):
    def test_l00_a_helper_without_a_usable_round_module_gives_none_before_anything(self):
        for name, spec in (("missing", dict(omit=("judge_rounds.py",))),
                           ("raises", dict(replace={"judge_rounds.py": "raise RuntimeError('x')\n"}))):
            with self.subTest(module=name):
                helper = self.bundle("jr-" + name, **spec)
                result = self.run_helper("claude", "gate", helper=helper)
                self.assertEqual((result.code, result.word, result.calls), (3, "none", []))
                self.assertEqual(result.note, "no verdict: the settings modules are not available")
                self.assert_no_records()
                self.assertIsNone(self.room())

    def test_l01_an_explicit_lineage_of_another_form_gives_invalid_arguments_in_every_mode(self):
        for mode in ("off", "gate", "each"):
            with self.subTest(mode=mode):
                self.write_settings({"judge": {"mode": mode}})
                for bad in ("CON-1", "con-20261117-01", "CON-20261117-011", "CON-20261117-01 "):
                    result = self.go(lineage=bad)
                    self.assertEqual((result.code, result.word, result.ran), (3, "none", 0), result.out)
                    self.assertTrue(result.note.startswith("no verdict: invalid arguments: "), result.note)
                    self.assertIn("lineage", result.note)
                self.assertFalse((self.common / "readback").exists())
                self.assertIsNone(self.room())

    def test_l02_a_record_of_off_and_skipped_holds_the_lineage_or_null(self):
        rows = (("off", "gate", base.CONTRACT_TRUE, None, LIN), ("gate", "step", base.CONTRACT_TRUE, None, LIN),
                ("off", "gate", "Title without an id\n", None, None), ("gate", "step", "Title without an id\n", None, None),
                ("off", "gate", "Title without an id\n", "CON-20261117-05", "CON-20261117-05"),
                ("off", "gate", base.CONTRACT_TRUE, "CON-20261117-05", "CON-20261117-05"))
        for mode, tier, contract, explicit, expected in rows:
            with self.subTest(mode=mode, tier=tier, expected=expected):
                shutil.rmtree(self.common / "readback", ignore_errors=True)
                self.put(contract, base.HANDOFF_TRUE)
                self.write_settings({"judge": {"mode": mode}})
                result = self.go(tier=tier, lineage=explicit)
                self.assertEqual((result.code, result.ran), (0, 0), result.out)
                record = self.records("no-review")
                self.assertEqual(len(record), 1)
                call = self.expected_call("claude", None, tier, mode, base.PF, "file")
                call["lineage"] = expected
                self.assertEqual(record[0]["call"], call)

    def test_l03_the_main_and_a_linked_worktree_share_a_ledger_other_homes_do_not(self):
        linked = self.base / "linked"
        self.git("worktree", "add", "-q", str(linked), "-b", "other")
        self.ok(self.go(), "PASS")
        result = self.go(repo=linked)
        self.assertEqual((result.code, result.ran, result.word), (0, 1, "PASS"), result.out)
        self.assertEqual(self.chain(repo=linked)[1], self.chain()[1])
        ledger = self.ledger()
        self.assertEqual(len(ledger["attempts"]), 2)
        self.assertEqual(ledger["repository"], str(self.common))
        self.assertEqual(len(self.room()), 2)
        second = self.base / "second"
        second.mkdir()
        self.assertEqual(subprocess.run(["git", "-C", str(second), "init", "-q"], capture_output=True).returncode, 0)
        result = self.go(repo=second)
        self.assertEqual((result.code, result.ran), (0, 1), result.out)
        self.assertNotEqual(self.chain(repo=second)[1], self.chain()[1])
        self.assertEqual(self.chain(repo=second)[0]["home"], str(self.home / "logbook" / "second"))
        self.assertEqual(len(self.room()), 4)
        self.assertEqual(len(self.ledger(repo=second)["attempts"]), 1)

    def test_l04_a_folder_outside_git_has_its_resolved_path_as_repository(self):
        plain = self.base / "plain"
        plain.mkdir()
        result = self.go(repo=plain)
        self.assertEqual((result.code, result.ran), (0, 1), result.out)
        ledger = self.ledger(repo=plain)
        self.assertEqual(ledger["repository"], str(plain.resolve()))
        self.assertEqual(ledger["project_home"], str(self.home / "logbook" / "plain"))

    def test_l05_the_keys_modes_and_content_of_a_ledger(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        self.ok(self.go(), "PASS")
        path = self.path()
        ledger = json.loads(path.read_text())
        self.assertEqual(sorted(ledger), ["attempts", "lineage", "project_home", "repository", "schema_version",
                                          "settings_digest", "tier"])
        self.assertEqual(sorted(ledger["attempts"][0]), ["attempt_id", "authorization", "deadline_seconds", "ended_by",
                                                         "finished_at", "reserved_at", "result", "route",
                                                         "settings_digest"])
        self.assertEqual(path.read_text(), json.dumps(ledger, sort_keys=True, indent=2) + "\n")
        mode = lambda item: stat.S_IMODE(item.stat().st_mode)  # noqa: E731
        self.assertEqual((mode(path), mode(path.with_suffix(".lock"))), (0o600, 0o600))
        self.assertEqual((mode(self.rounds_dir()), mode(self.rounds_dir().parent)), (0o700, 0o700))
        self.assertEqual(self.room(), sorted([path.name, path.with_suffix(".lock").name]))
        self.assertEqual(path.name, judge_rounds.chain_key(ledger["project_home"], ledger["repository"], LIN, "gate") + ".json")
        self.assertEqual(ledger["attempts"][0]["route"], {"provider": "codex", "model": "gpt-6-astra", "effort": "high",
                                                          "source": "defaults"})
        self.assertRegex(ledger["attempts"][0]["attempt_id"], r"^[0-9a-f]{32}$")

    def test_l06_a_failing_rename_at_the_reservation_keeps_the_earlier_ledger(self):
        self.fails(1)
        before = self.path().read_bytes()

        def fails(source, target):
            raise OSError("injected")
        result = self.go(patch=[(judge_rounds, "_rename", fails)])
        self.refused(result, "no verdict: the round accounting failed")
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(len(self.room()), 2)
        self.assertEqual(self.records("started"), [])

    def test_l07_the_round_object_of_the_started_and_the_completed_record(self):
        for _ in range(2):
            self.go(FAIL)
        self.ok(self.go(PASS), "PASS")
        self.fails(3)
        self.authorize()
        self.ok(self.go(PASS, sid="sess-1"), "PASS")
        expected = {1: {"attempt": 1, "fails_before": 0, "authorization": None},
                    3: {"attempt": 3, "fails_before": 2, "authorization": None},
                    4: {"attempt": 4, "fails_before": 3, "authorization": "auth-1"}}
        for kind in ("started", "completed"):
            found = {r["call"]["round"]["attempt"]: r["call"]["round"] for r in self.records(kind)}
            self.assertEqual(found.get(3), expected[3], kind)
            self.assertEqual(found.get(4), expected[4], kind)
            self.assertEqual(found.get(1), expected[1], kind)
        self.assertEqual({r["call"]["lineage"] for r in self.records("completed")}, {LIN})

    def test_l08_an_attempt_after_a_pin_change_stores_the_new_digest(self):
        self.ok(self.go(FAIL), "FAIL")
        old = self.digest()
        self.write_settings({"judge": {"mode": "gate", "deadline_seconds": {"step": 300, "gate": 700}}})
        self.authorize()
        self.ok(self.go(PASS, sid="sess-1"), "PASS")
        new = self.digest()
        ledger = self.ledger()
        self.assertEqual([a["settings_digest"] for a in ledger["attempts"]], [old, new])
        self.assertEqual((ledger["settings_digest"], ledger["attempts"][1]["deadline_seconds"]), (new, 700))
        second = [r for r in self.records("completed") if r["call"]["round"]["attempt"] == 2]
        self.assertEqual([r["call"]["settings_digest"] for r in second], [new])


if __name__ == "__main__":
    unittest.main(failfast=True)
