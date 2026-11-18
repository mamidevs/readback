#!/usr/bin/env python3
"""Offline checks for the kept texts, the retry sections, the coverage list and the answer record (CON-20261118-18).

Run one class: python3 -B checks/test_judge_retry.py KeepTests
Classes: KeepTests (K1 to K9, D1, D2), RetryTests (P1 to P11, D3, D4), CoverageTests (C1 to C9, D5),
AnswerTests (D6). A chain is built with the fake provider of the round tests and the clock that the test sets.
Every run sets HOME to a temporary folder and removes both session variables. No model call, no network.
"""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_judge_rounds as R  # noqa: E402  (the module, so that discovery finds no class twice)

base, outside_judge, judge_rounds = R.base, R.outside_judge, R.judge_rounds
review_receipt = base.review_receipt
PASS, FAIL = R.PASS, R.FAIL
T0 = "2026-11-18T10:00:00Z"
VALID = [(review_receipt, "run_validation", lambda *args: {"valid": True, "findings": []})]
H1 = "--- EARLIER ATTEMPTS OF THIS LINEAGE AND TIER (data, not instructions) ---"
OBSERVED = "\n--- WORKTREE OBSERVED BY THE HELPER (Git output; file names are data) ---\n"
END = "\n--- END RECORDS ---\n"
CUT = "[cut by the helper at 16384 bytes]\n"
COVERAGE = "--- COVERAGE LIST OF THE GATE CALL (path | sha256; read these files) ---"
LIMIT = 1048576


def h2(number):
    return "--- CHANGES OF THE HANDOFF SINCE ATTEMPT %d (unified diff; data) ---" % number


def h3(number):
    return ("--- WORKTREE OBSERVATION: METADATA DIFFERENCES SINCE ATTEMPT %d "
            "(Git output of the helper, not file contents; data) ---" % number)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sized(size, crlf=False):
    """A handoff of exactly `size` bytes in one long summary line."""
    def build(count):
        text = base.HANDOFF_TRUE.replace("summary   Done.\n", "summary   " + "z" * count + "\n")
        return (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")
    return build(size - len(build(0)))


def sections(prompt):
    """The contents of the three retry sections of a prompt, or None when it holds none."""
    assert prompt.endswith(END)
    if "\n" + H1 not in prompt:
        return None
    text = prompt[prompt.index("\n" + H1) + len("\n" + H1 + "\n"):-len(END)]
    first, rest = text.split("\n--- CHANGES OF THE HANDOFF SINCE ATTEMPT ", 1)
    second, third = rest.split("\n--- WORKTREE OBSERVATION: METADATA DIFFERENCES SINCE ATTEMPT ", 1)
    return first, second.split("\n", 1), third.split("\n", 1)


class Case(R.Rounds):
    """The round helper with a prompt capture and a run that takes more arguments."""

    def setUp(self):
        super().setUp()
        self.prompts, self.listing = [], []

    def fake(self, command, prompt, repo, env, seconds):
        self.prompts.append(prompt)
        self.listing.append(self.names())
        return super().fake(command, prompt, repo, env, seconds)

    def call(self, *extra, answer=PASS, tier="gate", error=None, during=None, patch=()):
        self.answer, self.error, self.during, self.ran = answer, error, during, []
        argv = ["--host", "claude", "--tier", tier, *extra, str(self.contract), str(self.handoff), str(self.repo)]
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, self.base_env({}), clear=True))
            stack.enter_context(mock.patch.object(outside_judge, "run_process", self.fake))
            for target, name, value in patch:
                stack.enter_context(mock.patch.object(target, name, value))
            out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = outside_judge.main(argv)
        lines = out.getvalue().split("\n")
        parts = lines[0].split(" | ", 3)
        return types.SimpleNamespace(code=code, out=out.getvalue(), lines=lines, ran=len(self.ran),
                                     note=parts[3] if len(parts) == 4 else None, word=parts[0].split()[1])

    def attempt_dir(self, number):
        return self.rounds_dir() / self.path().name[:-5] / self.ledger()["attempts"][number - 1]["attempt_id"]

    def names(self):
        ledger = self.ledger()
        if ledger is None:
            return None
        folder = self.attempt_dir(len(ledger["attempts"]))
        return sorted(os.listdir(folder)) if folder.is_dir() else None

    def line(self, number, result, finished=T0):
        return "attempt %d | %s | %s | %s\n" % (number, result, finished, self.attempt_dir(number))

    def observation(self):
        return outside_judge.worktree_observation(self.repo)

    def stored(self, kind):
        return self.records(kind)

    def refusal(self, result, note):
        self.assertEqual((result.code, result.ran, result.word, result.note), (3, 0, "none", note), result.out)
        self.assertIsNone(self.ledger())
        self.assertIsNone(self.room())
        self.assert_no_records()


# ---------------------------------------------------------------------------------------------- KeepTests

class KeepTests(Case):
    def test_k01_a_first_fail_keeps_three_files_with_their_bytes_and_modes(self):
        result = self.call(answer=FAIL)
        self.assertEqual((result.code, result.ran, result.word), (0, 1, "FAIL"))
        folder = self.attempt_dir(1)
        self.assertEqual(sorted(os.listdir(folder)), ["answer.txt", "handoff.txt", "observation.txt"])
        self.assertEqual((folder / "handoff.txt").read_bytes(), self.handoff.read_bytes())
        self.assertEqual((folder / "observation.txt").read_bytes(), self.observation().encode("utf-8"))
        self.assertEqual((folder / "answer.txt").read_bytes(), FAIL.encode("utf-8"))
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(folder.parent.stat().st_mode), 0o700)
        for name in ("handoff.txt", "observation.txt", "answer.txt"):
            self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600, name)
        self.assertEqual(sorted(item.name for item in self.rounds_dir().iterdir() if item.is_dir()), [folder.parent.name])
        self.assertEqual(self.snap(), self.expect(self.att("FAIL")))

    def test_k02_handoff_and_observation_exist_at_the_start_and_the_answer_at_the_terminal_write(self):
        seen = []
        real = judge_rounds.confirm
        finish = judge_rounds.finish

        def confirm(reservation):
            seen.append(("confirm", self.names()))
            return real(reservation)

        def terminal(reservation, verdict):
            seen.append(("finish", self.names()))
            return finish(reservation, verdict)
        self.call(patch=[(judge_rounds, "confirm", confirm), (judge_rounds, "finish", terminal)])
        self.assertEqual(self.listing, [["handoff.txt", "observation.txt"]])
        self.assertEqual(seen, [("confirm", ["handoff.txt", "observation.txt"]),
                                ("finish", ["answer.txt", "handoff.txt", "observation.txt"])])

    def test_k03_a_provider_error_and_a_missing_verdict_keep_no_answer(self):
        result = self.call(error="the judge command ended with code 1")
        self.assertEqual((result.code, result.ran), (3, 1))
        self.assertEqual(sorted(os.listdir(self.attempt_dir(1))), ["handoff.txt", "observation.txt"])
        result = self.call(answer="The handoff is fine.\n")
        self.assertEqual((result.code, result.note), (3, "no verdict: the answer does not start with PASS or FAIL"))
        self.assertEqual(sorted(os.listdir(self.attempt_dir(2))), ["handoff.txt", "observation.txt"])
        self.assertEqual(self.snap(), self.expect(self.att("none"), self.att("none")))

    def test_k04_a_handoff_of_one_mebibyte_is_kept_and_one_byte_more_is_refused(self):
        data = sized(LIMIT)
        self.assertEqual(len(data), LIMIT)
        self.handoff.write_bytes(data)
        result = self.call(patch=VALID)
        self.assertEqual((result.code, result.ran), (0, 1))
        self.assertEqual((self.attempt_dir(1) / "handoff.txt").read_bytes(), data)
        self.forget_rounds()
        shutil_rmtree(self.common / "readback")
        self.handoff.write_bytes(sized(LIMIT + 1))
        result = self.call(patch=VALID)
        self.refusal(result, "no verdict: the handoff is too large to keep")
        self.assertEqual(self.ran, [])

    def test_k04b_the_size_of_a_handoff_counts_the_bytes_of_its_file_before_its_line_ends_change(self):
        data = sized(LIMIT + 1, crlf=True)
        self.assertLess(len(data.replace(b"\r\n", b"\n")), LIMIT)
        self.handoff.write_bytes(data)
        self.refusal(self.call(patch=VALID), "no verdict: the handoff is too large to keep")
        data = sized(LIMIT, crlf=True)
        self.handoff.write_bytes(data)
        self.assertEqual(self.call(patch=VALID).code, 0)
        self.assertEqual((self.attempt_dir(1) / "handoff.txt").read_bytes(), data)

    def test_k05_an_observation_above_the_limit_is_refused_and_one_at_the_limit_is_kept(self):
        note = "no verdict: the worktree observation is too large to keep"
        limit = "é" * (LIMIT // 2)
        self.assertEqual(len(limit.encode("utf-8")), LIMIT)
        patch = [(outside_judge, "worktree_observation", lambda repo: limit + "x")]
        self.refusal(self.call(patch=patch), note)
        patch = [(outside_judge, "worktree_observation", lambda repo: limit)]
        self.assertEqual(self.call(patch=patch).code, 0)
        self.assertEqual((self.attempt_dir(1) / "observation.txt").read_bytes(), limit.encode("utf-8"))

    def test_k06_an_answer_above_the_limit_gives_none_and_keeps_no_answer(self):
        big = "PASS " + "a" * (LIMIT - 5 + 1)
        self.assertEqual(len(big.encode("utf-8")), LIMIT + 1)
        result = self.call(answer=big)
        self.assertEqual((result.code, result.ran, result.word, result.note), (3, 1, "none",
                                                                          "no verdict: the answer is too large to keep"))
        self.assertEqual(sorted(os.listdir(self.attempt_dir(1))), ["handoff.txt", "observation.txt"])
        self.assertEqual(self.snap(), self.expect(self.att("none")))
        completed = self.stored("completed")[0]
        self.assertEqual((completed["answer"], completed["answer_sha256"], completed["exit_status"]), (None, None, 3))
        exact = "PASS " + "a" * (LIMIT - 5)
        result = self.call(answer=exact)
        self.assertEqual((result.code, result.word), (0, "PASS"))
        self.assertEqual(len((self.attempt_dir(2) / "answer.txt").read_bytes()), LIMIT)

    def test_k07_an_attempt_folder_that_cannot_be_written_ends_the_attempt_without_a_provider(self):
        self.rounds_dir().mkdir(parents=True)
        (self.rounds_dir() / self.path().name[:-5]).write_text("a file where the folder belongs")
        result = self.call()
        self.assertEqual((result.code, result.ran, result.word, result.note), (3, 0, "none",
                                                                          "no verdict: the round accounting failed"))
        self.assertEqual(self.snap(), self.expect(self.att("none", by="helper")))
        self.assertEqual(self.prompts, [])

    def test_k08_an_answer_that_cannot_be_written_leaves_the_verdict(self):
        def block():
            (self.attempt_dir(1) / "answer.txt").mkdir()
        result = self.call(answer=FAIL, during=block)
        self.assertEqual((result.code, result.ran, result.word), (0, 1, "FAIL"))
        self.assertEqual(self.snap(), self.expect(self.att("FAIL")))
        self.assertEqual(self.stored("completed")[0]["answer"], FAIL)
        self.call()
        self.assertEqual(sections(self.prompts[1])[0], self.line(1, "FAIL") + "not available\n")

    def test_k09_a_handoff_with_crlf_and_a_letter_outside_ascii_is_kept_byte_for_byte(self):
        data = base.HANDOFF_TRUE.replace("Done.", "Done é.").replace("\n", "\r\n").encode("utf-8")
        self.handoff.write_bytes(data)
        self.assertEqual(self.call().code, 0)
        self.assertEqual((self.attempt_dir(1) / "handoff.txt").read_bytes(), data)


def shutil_rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------------------------- RetryTests

SUMMARY = base.HANDOFF_TRUE.replace("summary   Done.", "summary   Done again.")
DIFF_SUMMARY = ("--- attempt-1/handoff.txt\n+++ current/handoff.txt\n@@ -3,7 +3,7 @@\n date      2026-11-17\n"
                " state     DONE\n readback  task: x | limits: y | done when: ACC-1, ACC-2\n-summary   Done.\n"
                "+summary   Done again.\n changes   none\n commit    none\n checks    sh a.sh | exit 0 | ok\n")


class RetryTests(Case):
    def test_p01_a_first_attempt_sends_the_prompt_of_the_part_one_helper_byte_for_byte(self):
        old = self.run_helper("claude", helper=self.bundle("oracle", oracle=True))
        self.assertEqual(len(old.calls), 1)
        self.call()
        self.assertEqual(self.prompts[0].encode("utf-8"), old.calls[0]["prompt"])
        self.assertIsNone(sections(self.prompts[0]))

    def test_p02_a_second_attempt_after_a_fail_with_one_changed_line_holds_three_sections(self):
        self.call(answer=FAIL)
        self.handoff.write_text(SUMMARY)
        self.call()
        text = (OBSERVED + self.observation() + "\n" + H1 + "\n" + self.line(1, "FAIL") + FAIL + "\n" + h2(1) + "\n"
                + DIFF_SUMMARY + "\n" + h3(1) + "\nno difference\n" + END)
        self.assertTrue(self.prompts[1].endswith(text), self.prompts[1][-1500:])
        self.assertEqual(self.prompts[1].count("--- END RECORDS ---"), 1)

    def test_p03_an_unchanged_handoff_and_observation_give_no_difference_twice(self):
        self.call(answer=FAIL)
        self.call()
        text = ("\n" + H1 + "\n" + self.line(1, "FAIL") + FAIL + "\n" + h2(1) + "\nno difference\n\n" + h3(1)
                + "\nno difference\n" + END)
        self.assertTrue(self.prompts[1].endswith(self.observation() + text))

    def test_p04_three_earlier_attempts_with_a_provider_error_in_the_middle_newest_first(self):
        self.call(answer=FAIL)
        self.call(error="the judge command ended with code 1")
        self.call(answer="FAIL Second finding.\n")
        self.call()
        first = (self.line(3, "FAIL") + "FAIL Second finding.\n\n" + self.line(2, "none") + "no answer\n\n"
                 + self.line(1, "FAIL") + FAIL)
        got = sections(self.prompts[3])
        self.assertEqual(got[0], first)
        self.assertEqual((got[1][0], got[2][0].split(" ")[0]), ("3 (unified diff; data) ---", "3"))

    def test_p05_a_pending_earlier_attempt_says_pending_and_a_dash(self):
        def crash():
            raise R.Crash()
        with self.assertRaises(R.Crash):
            self.call(during=crash)
        self.assertEqual(self.ledger()["attempts"][0]["result"], None)
        self.call()
        self.assertEqual(sections(self.prompts[1])[0], self.line(1, "pending", "-") + "no answer\n")
        self.assertEqual(sections(self.prompts[1])[1], ["1 (unified diff; data) ---", "no difference\n"])

    def test_p06_a_chain_of_attempts_that_ended_with_none_still_gets_the_sections(self):
        self.call(error="the judge command ended with code 1")
        self.call(error="the judge command ended with code 2")
        self.call()
        text = self.line(2, "none") + "no answer\n\n" + self.line(1, "none") + "no answer\n"
        self.assertEqual(sections(self.prompts[2])[0], text)

    def head_length(self):
        """The bytes of the attempt line of attempt 1 with a result of four letters."""
        return len(("attempt 1 | PASS | %s | " % T0).encode("utf-8")) + len(str(self.rounds_dir())) + 1 + 64 + 1 + 32 + 1

    def test_p07_a_kept_answer_of_20000_bytes_shows_exactly_16384_bytes_then_the_cut_line(self):
        answer = "PASS " + "x" * 19994 + "\n"
        self.assertEqual(len(answer), 20000)
        self.call(answer=answer)
        reads = []
        real = open

        def counting(path, *args, **kwargs):
            handle = real(path, *args, **kwargs)
            if str(path).endswith("answer.txt"):
                self.assertIs(type(handle), io.FileIO)
                inner = handle.read

                def read(count=-1):
                    data = inner(count)
                    reads.append(len(data))
                    return data
                handle.read = read
            return handle
        with mock.patch.object(judge_rounds, "open", counting, create=True):
            self.call()
        first = sections(self.prompts[1])[0]
        head = self.line(1, "PASS")
        room = 16384 - len(head)
        self.assertEqual(first, head + answer[:room] + "\n" + CUT)
        self.assertEqual(len((first[:-len(CUT) - 1]).encode("utf-8")), 16384)
        self.assertTrue(room <= sum(reads) <= room + 1, (reads, room))

    def test_p07b_an_answer_that_fills_the_budget_exactly_has_no_cut_line_and_one_byte_more_has(self):
        room = 16384 - self.head_length()
        for extra, cut in ((0, False), (1, True)):
            self.forget_rounds()
            answer = "PASS " + "y" * (room - 6 + extra) + "\n"
            self.call(answer=answer)
            self.call()
            first, head = sections(self.prompts[-1])[0], self.line(1, "PASS")
            if cut:
                self.assertEqual(first, head + answer[:room] + "\n" + CUT)
            else:
                self.assertEqual(first, head + answer)
                self.assertEqual(len(first.encode("utf-8")), 16384)

    def test_p07c_a_multibyte_letter_is_never_split_at_the_cut(self):
        answer = "PASS " + "é" * 10000 + "\n"
        self.call(answer=answer)
        self.call()
        first = sections(self.prompts[1])[0]
        head = self.line(1, "PASS")
        room = 16384 - len(head.encode("utf-8"))
        shown = (room - 5) // 2
        self.assertEqual(first, head + "PASS " + "é" * shown + "\n" + CUT)
        self.assertEqual(len((head + "PASS " + "é" * shown).encode("utf-8")), 16384 - (room - 5) % 2)

    def test_p07d_nothing_is_added_after_a_cut_that_leaves_spare_bytes(self):
        self.call(error="the judge command ended with code 1")
        head = self.head_length()
        pad = (16384 - head - 5 - 3) % 4
        answer = "PASS " + "a" * pad + "\U0001F600" * 5000 + "\n"
        self.call(answer=answer)
        self.call()
        first = sections(self.prompts[2])[0]
        room = 16384 - len(self.line(2, "PASS").encode("utf-8"))
        shown = (room - 5 - pad) // 4
        self.assertEqual(room - 5 - pad - 4 * shown, 3)
        self.assertEqual(first, self.line(2, "PASS") + "PASS " + "a" * pad + "\U0001F600" * shown + "\n" + CUT)

    def test_p07e_an_exact_fit_followed_by_an_older_block_gives_the_cut_line_directly(self):
        room = 16384 - self.head_length()
        self.call(error="the judge command ended with code 1")
        answer = "PASS " + "y" * (room - 6) + "\n"
        self.call(answer=answer)
        self.call()
        self.assertEqual(sections(self.prompts[2])[0], self.line(2, "PASS") + answer + CUT)

    def test_p07f_an_answer_without_a_final_line_end_gets_one_before_the_next_block(self):
        self.call(answer="PASS no final line end")
        self.call(answer=FAIL)
        self.call()
        self.assertEqual(sections(self.prompts[2])[0], self.line(2, "FAIL") + FAIL + "\n" + self.line(1, "PASS")
                         + "PASS no final line end\n")

    def test_p07g_a_diff_line_without_a_line_end_gets_one(self):
        self.handoff.write_text(base.HANDOFF_TRUE.rstrip("\n"))
        self.call()
        self.handoff.write_text(base.HANDOFF_TRUE.rstrip("\n").replace("Accept.", "Accept it."))
        self.call()
        got = sections(self.prompts[1])[1][1]
        self.assertTrue(got.endswith("\n-next      Accept.\n+next      Accept it.\n"), got)

    def test_p10c_a_kept_handoff_of_exactly_262144_bytes_is_compared(self):
        self.handoff.write_bytes(sized(262144))
        self.call(patch=VALID)
        self.handoff.write_bytes(sized(400))
        self.call(patch=VALID)
        got = sections(self.prompts[1])[1][1]
        self.assertTrue(got.startswith("--- attempt-1/handoff.txt\n+++ current/handoff.txt\n@@ "), got[:200])
        self.assertTrue(got.endswith("\n" + CUT))

    def test_p08_twelve_earlier_attempts_show_the_newest_blocks_up_to_the_limit_then_the_cut_line(self):
        answers = []
        for number in range(1, 13):
            answers.append("PASS %02d " % number + "k" * (2000 - 8) + "\n")
            self.call(answer=answers[-1])
        self.call()
        blocks = [self.line(number, "PASS") + answers[number - 1] for number in range(12, 0, -1)]
        wanted = "\n".join(blocks)[:16384]
        self.assertEqual(sections(self.prompts[12])[0], wanted + "\n" + CUT)
        self.assertTrue(2 < wanted.count("attempt ") < 12)

    def test_p09_a_removed_kept_handoff_gives_not_available_and_the_observation_is_unaffected(self):
        self.call()
        (self.attempt_dir(1) / "handoff.txt").unlink()
        self.call()
        self.assertEqual(sections(self.prompts[1])[1], ["1 (unified diff; data) ---", "not available\n"])
        self.assertEqual(sections(self.prompts[1])[2], ["1 (Git output of the helper, not file contents; data) ---",
                                                        "no difference\n"])

    def test_p09b_the_newest_earlier_attempt_with_a_kept_text_is_the_one_compared(self):
        for _ in range(3):
            self.call()
        (self.attempt_dir(3) / "handoff.txt").unlink()
        (self.attempt_dir(2) / "handoff.txt").unlink()
        (self.attempt_dir(3) / "observation.txt").unlink()
        self.handoff.write_text(SUMMARY)
        self.call()
        got = sections(self.prompts[3])
        self.assertEqual(got[1], ["1 (unified diff; data) ---", DIFF_SUMMARY])
        self.assertEqual(got[2], ["2 (Git output of the helper, not file contents; data) ---", "no difference\n"])

    def test_p10_a_current_handoff_above_262144_bytes_gives_the_too_large_line_with_both_hashes(self):
        self.call()
        kept = self.handoff.read_bytes()
        for size, large in ((262144, False), (262145, True)):
            self.forget_rounds()
            self.handoff.write_bytes(kept)
            self.call()
            current = sized(size)
            self.handoff.write_bytes(current)
            self.call(patch=VALID)
            got = sections(self.prompts[-1])
            if large:
                self.assertEqual(got[1], ["1 (unified diff; data) ---",
                                          "too large to compare (sha256 %s, %s)\n" % (sha(kept), sha(current))])
            else:
                self.assertTrue(got[1][1].startswith("--- attempt-1/handoff.txt\n+++ current/handoff.txt\n"))
                self.assertTrue(got[1][1].endswith("\n" + CUT))
                self.assertEqual(len(got[1][1][:-len(CUT) - 1].encode("utf-8")), 16384)
            self.assertEqual(got[2][1], "no difference\n")

    def test_p10b_a_kept_handoff_above_262144_bytes_is_hashed_in_chunks(self):
        self.handoff.write_bytes(sized(262145))
        self.call(patch=VALID)
        kept = self.handoff.read_bytes()
        self.handoff.write_bytes(sized(400))
        self.call(patch=VALID)
        self.assertEqual(sections(self.prompts[1])[1],
                         ["1 (unified diff; data) ---",
                          "too large to compare (sha256 %s, %s)\n" % (sha(kept), sha(self.handoff.read_bytes()))])

    def test_p11_an_observation_that_differs_in_one_status_line_shows_that_line(self):
        self.call()
        (self.repo / "new.txt").write_text("new\n")
        self.call()
        got = sections(self.prompts[1])
        self.assertEqual(got[1][1], "no difference\n")
        head, body = got[2]
        self.assertEqual(head, "1 (Git output of the helper, not file contents; data) ---")
        lines = body.split("\n")
        self.assertEqual(lines[:2], ["--- attempt-1/observation.txt", "+++ current/observation.txt"])
        self.assertEqual(lines[2], "@@ -4,6 +4,7 @@")
        self.assertEqual(lines[3:], [" ", " $ git status --porcelain=v1 --untracked-files=all -- .", " exit 0",
                                     "+?? new.txt", " ", " $ git diff --no-ext-diff --no-textconv --stat -- .",
                                     " exit 0", ""])


    def test_p12_a_ledger_without_the_current_attempt_gives_the_accounting_note(self):
        self.call()
        reservation = types.SimpleNamespace(folder=str(self.rounds_dir()), path=str(self.path()),
                                            identity={}, attempt_id="gone")
        with mock.patch.object(judge_rounds, "read_ledger", lambda path, identity: self.ledger()):
            with self.assertRaises(judge_rounds.RoundError) as caught:
                judge_rounds.earlier_attempts(reservation)
        self.assertEqual(caught.exception.note, "the round accounting failed")
        with mock.patch.object(judge_rounds, "read_ledger", lambda path, identity: None):
            with self.assertRaises(judge_rounds.RoundError):
                judge_rounds.earlier_attempts(reservation)

    def test_p13_a_kept_text_is_never_read_into_a_comparison_beyond_the_chunks_of_the_hash(self):
        self.handoff.write_bytes(sized(262145 + 70000))
        self.call(patch=VALID)
        kept = self.handoff.read_bytes()
        reads = []
        real = open

        def counting(path, *args, **kwargs):
            handle = real(path, *args, **kwargs)
            if str(path).endswith("handoff.txt"):
                self.assertIs(type(handle), io.FileIO)
                inner = handle.read

                def read(count=-1):
                    data = inner(count)
                    reads.append((count, len(data)))
                    return data
                handle.read = read
            return handle
        with mock.patch.object(judge_rounds, "open", counting, create=True):
            self.call(patch=VALID)
        self.assertEqual(reads[0], (262145, 262145))
        self.assertEqual(sum(size for _count, size in reads), len(kept))
        self.assertTrue(all(count <= 65536 for count, _size in reads[1:]), reads)


# ------------------------------------------------------------------------------------------- CoverageTests

class CoverageTests(Case):
    def setUp(self):
        super().setUp()
        self.folder = self.base / "inputs"
        self.listing_file = self.folder / "coverage.txt"

    def files(self, **named):
        for name, data in named.items():
            (self.folder / name).write_bytes(data)

    def cover(self, text, *extra, **kw):
        self.listing_file.write_text(text)
        return self.call("--coverage", str(self.listing_file), *extra, **kw)

    def test_c01_a_list_with_two_names_one_relative_gives_two_lines_and_the_call_list(self):
        outside = self.base / "outside.txt"
        outside.write_bytes(b"outside\n")
        self.files(**{"rel.txt": b"relative\n"})
        result = self.cover("rel.txt\n%s\n" % outside)
        self.assertEqual((result.code, result.ran), (0, 1), result.out)
        want = [{"path": str(self.folder / "rel.txt"), "sha256": sha(b"relative\n")},
                {"path": str(outside), "sha256": sha(b"outside\n")}]
        text = "\n" + COVERAGE + "\n" + "".join("%s | %s\n" % (item["path"], item["sha256"]) for item in want)
        handoff = "--- HANDOFF ---\n" + self.handoff.read_text()
        self.assertIn(handoff + text + OBSERVED, self.prompts[0])
        self.assertEqual(self.prompts[0].count(COVERAGE), 1)
        self.assertEqual(self.stored("started")[0]["call"]["coverage"], want)
        self.assertEqual(self.stored("completed")[0]["call"]["coverage"], want)
        self.assertEqual(list(self.stored("completed")[0]["call"]), list(self.stored("started")[0]["call"]))

    def test_c01b_the_coverage_section_stands_before_the_validation_section_and_after_the_handoff(self):
        self.files(**{"rel.txt": b"relative\n"})
        self.handoff.write_text(base.HANDOFF_UNPARSED)
        self.cover("rel.txt\n")
        prompt = self.prompts[0]
        self.assertLess(prompt.index("--- HANDOFF ---"), prompt.index(COVERAGE))
        self.assertLess(prompt.index(COVERAGE), prompt.index("--- STRUCTURAL VALIDATION BY THE HELPER"))
        self.assertLess(prompt.index("--- STRUCTURAL VALIDATION BY THE HELPER"), prompt.index(OBSERVED))

    def test_c01c_a_later_attempt_keeps_the_coverage_section_before_the_retry_sections(self):
        self.files(**{"rel.txt": b"relative\n"})
        self.cover("rel.txt\n", answer=FAIL)
        self.cover("rel.txt\n")
        prompt = self.prompts[1]
        self.assertLess(prompt.index(COVERAGE), prompt.index(OBSERVED))
        self.assertLess(prompt.index(OBSERVED), prompt.index(H1))

    def test_c02_coverage_with_the_step_tier_is_refused_as_an_invalid_argument(self):
        self.files(**{"rel.txt": b"x"})
        result = self.cover("rel.txt\n", tier="step")
        self.refusal(result, "no verdict: invalid arguments: --coverage is allowed with --tier gate only")

    def test_c03_a_named_file_that_is_missing_is_refused(self):
        self.refusal(self.cover("gone.txt\n"), "no verdict: cannot read the coverage list: a named file")

    def test_c04_a_list_with_only_blank_lines_is_refused(self):
        note = "no verdict: cannot read the coverage list: no entry"
        self.refusal(self.cover("\n  \n\t\n"), note)
        self.refusal(self.cover(""), note)

    def test_c05_blank_lines_padded_names_and_a_space_inside_a_name(self):
        self.files(**{"one.txt": b"1", "my file.txt": b"2"})
        result = self.cover("\n   \n  one.txt  \r\nmy file.txt\n\t\n")
        self.assertEqual((result.code, result.ran), (0, 1), result.out)
        self.assertEqual([item["path"] for item in self.stored("started")[0]["call"]["coverage"]],
                         [str(self.folder / "one.txt"), str(self.folder / "my file.txt")])

    def test_c06_no_coverage_option_gives_no_section_and_no_key(self):
        self.call()
        self.assertNotIn("COVERAGE LIST", self.prompts[0])
        self.assertNotIn("coverage", self.stored("started")[0]["call"])
        self.assertNotIn("coverage", self.stored("completed")[0]["call"])

    def test_c07_a_list_file_that_is_missing_or_unreadable_is_refused(self):
        note = "no verdict: cannot read the coverage list: the list"
        self.refusal(self.call("--coverage", str(self.folder / "none.txt")), note)
        self.refusal(self.call("--coverage", str(self.folder)), note)
        self.listing_file.write_bytes(b"\xff\xfe\n")
        self.refusal(self.call("--coverage", str(self.listing_file)), note)

    def test_c08_a_list_with_65_names_is_refused_and_64_are_read(self):
        names = ["f%02d.txt" % number for number in range(65)]
        self.files(**{name: name.encode() for name in names})
        self.refusal(self.cover("\n".join(names) + "\n"), "no verdict: cannot read the coverage list: too many entries")
        result = self.cover("\n".join(names[:64]) + "\n")
        self.assertEqual((result.code, result.ran), (0, 1), result.out)
        self.assertEqual(len(self.stored("started")[0]["call"]["coverage"]), 64)

    def test_c09_one_name_twice_is_refused(self):
        self.files(**{"a.txt": b"a"})
        note = "no verdict: cannot read the coverage list: a repeated entry"
        self.refusal(self.cover("a.txt\na.txt\n"), note)
        self.refusal(self.cover("a.txt\n./a.txt\n"), note)
        self.refusal(self.cover("gone.txt\ngone.txt\n"), note)

    def test_c10_each_named_file_is_opened_once(self):
        self.files(**{"a.txt": b"a", "b.txt": b"b"})
        opened = []
        real = open

        def counting(path, *args, **kwargs):
            opened.append(str(path))
            return real(path, *args, **kwargs)
        with mock.patch.object(outside_judge, "open", counting, create=True):
            self.cover("a.txt\nb.txt\n")
        self.assertEqual(sorted(item for item in opened if item.startswith(str(self.folder))),
                         [str(self.folder / "a.txt"), str(self.folder / "b.txt")])

    def test_c11_a_large_named_file_is_hashed_whole_in_chunks_of_65536_bytes(self):
        data = bytes(range(256)) * 1000
        self.files(**{"big.bin": data})
        counts = []
        real = open

        def counting(path, *args, **kwargs):
            handle = real(path, *args, **kwargs)
            if str(path).endswith("big.bin"):
                inner = handle.read

                def read(count=-1):
                    counts.append(count)
                    return inner(count)
                handle.read = read
            return handle
        with mock.patch.object(outside_judge, "open", counting, create=True):
            self.cover("big.bin\n")
        self.assertEqual(self.stored("started")[0]["call"]["coverage"][0]["sha256"], sha(data))
        self.assertEqual(counts, [65536] * 5)


# --------------------------------------------------------------------------------------------- AnswerTests

class AnswerTests(Case):
    def test_a01_the_completed_record_of_a_verdict_holds_the_answer_text_beside_its_hash(self):
        text = "FAIL The line é is wrong.\r\nSecond line.\n"
        self.call(answer=text)
        record = self.stored("completed")[0]
        self.assertEqual(record["answer"], text.replace("\r\n", "\n"))
        self.assertEqual(record["answer_sha256"], sha(record["answer"].encode("utf-8")))
        self.assertEqual(record["answer"].encode("utf-8"), (self.attempt_dir(1) / "answer.txt").read_bytes())
        self.call()
        second = [item for item in self.stored("completed") if item["call"]["round"]["attempt"] == 2]
        self.assertEqual([item["answer"] for item in second], [PASS])

    def test_a02_a_completed_record_without_a_verdict_holds_answer_null(self):
        self.call(error="the judge command ended with code 1")
        record = self.stored("completed")[0]
        self.assertIn("answer", record)
        self.assertEqual((record["answer"], record["answer_sha256"], record["exit_status"]), (None, None, 3))

    def test_a03_a_record_of_part_one_without_the_key_stays_valid_for_the_scan(self):
        self.call()
        record = self.stored("completed")[0]
        folder = self.common / "readback" / "reviews" / record["review_key"]
        old = dict(record)
        del old["answer"]
        name = "0" * 32
        old["attempt_id"] = name
        (folder / (name + ".completed.json")).write_bytes(review_receipt.encode_record(old))
        started, completed, partial = review_receipt._scan(str(folder))
        self.assertIn(name, completed)
        self.assertIn(record["attempt_id"], completed)
        self.assertEqual(review_receipt.duplicates_of(str(self.common), record["review_key"], record["attempt_id"]),
                         [name])

    def test_a04_the_builder_gives_null_without_an_answer_argument(self):
        started = {"attempt_id": "a"}
        done = review_receipt.completed_record(started, {}, "b", {}, {}, None, 3, [])
        self.assertIsNone(done["answer"])
        done = review_receipt.completed_record(started, {}, "b", {}, {}, "d", 0, [], "text")
        self.assertEqual((done["answer"], done["answer_sha256"]), ("text", "d"))


if __name__ == "__main__":
    unittest.main(failfast=True)
