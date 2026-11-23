#!/usr/bin/env python3
"""Offline checks for scripts/closure_note.py (CON-20261123-08).

Run: python3 -m unittest discover -s checks -p 'test_closure_note.py' -v
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(os.environ.get("CLOSURE_ROOT") or Path(__file__).resolve().parent.parent)
SCRIPT = ROOT / "scripts" / "closure_note.py"

HND = "HND-20261123-17"
CON = "CON-20261123-08"
NOTE = "NOTE-20261123-03"
ENVELOPE = "<!-- readback record=NOTE-20261123-04 sha=abc -->"

HELPERS = {
    "pass": [
        "JUDGE      PASS | codex:gpt-5 | readback@abc1234 | all five hold",
        "execution  codex | gpt-5 | high | cli | run-7",
        "receipt    att-1 | ends-equal | validation true | /tmp/review/att-1.json",
    ],
    "fail": [
        "JUDGE      FAIL | codex:gpt-5 | readback@abc1234 | ACC-3 has no test",
        "execution  codex | gpt-5 | high | cli | run-8",
        "receipt    att-2 | unbound: head moved | validation false 1 | /tmp/review/att-2.json",
    ],
    "none": [
        "JUDGE      none | codex:gpt-5 | none | no verdict: timeout",
        "execution  codex | gpt-5 | high | cli | none",
        "receipt    none | no record was stored",
    ],
    "off": [
        "JUDGE      off | none:none | none | judge mode off (settings); no provider call",
        "execution  none | none | none | cli | none",
        "receipt    none | no record was stored",
    ],
}


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT)] + [str(a) for a in args],
                          capture_output=True, text=True, timeout=60)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def helper_file(self, kind, extra=()):
        return self.write("helper-%s.txt" % kind, "\n".join(HELPERS[kind] + list(extra)) + "\n")

    def review(self, kind="pass", extra=()):
        res = run("review", "--handoff", HND, "--contract", CON,
                  "--helper-output", self.helper_file(kind, extra))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        return res.stdout

    def acceptance(self, *extra, review=NOTE, accepted="yes"):
        res = run("acceptance", "--handoff", HND, "--contract", CON, "--review", review,
                  "--accepted", accepted, "--date", "2026-11-23", *extra)
        return res

    def check_text(self, text, name="note.txt"):
        res = run("check", self.write(name, text))
        return res, (json.loads(res.stdout) if res.stdout else None)

    def findings(self, text):
        res, obj = self.check_text(text)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertFalse(obj["valid"])
        return obj["findings"]


class ReviewTests(Base):
    def test_review_note_with_pass(self):
        self.assertEqual(self.review("pass"), "\n".join(
            ["kind review", "handoff " + HND, "contract " + CON] + HELPERS["pass"]) + "\n")

    def test_review_note_with_fail(self):
        self.assertEqual(self.review("fail"), "\n".join(
            ["kind review", "handoff " + HND, "contract " + CON] + HELPERS["fail"]) + "\n")

    def test_review_note_with_none(self):
        self.assertEqual(self.review("none"), "\n".join(
            ["kind review", "handoff " + HND, "contract " + CON] + HELPERS["none"]) + "\n")

    def test_review_note_with_second_receipt_form(self):
        out = self.review("none")
        self.assertEqual(out.split("\n")[5], "receipt    none | no record was stored")

    def test_review_note_with_off(self):
        self.assertEqual(self.review("off").split("\n")[4], "execution  none | none | none | cli | none")

    def test_review_keeps_only_first_three_helper_lines(self):
        out = self.review("pass", extra=["the full answer follows", "more text"])
        self.assertEqual(out.count("\n"), 6)
        self.assertNotIn("the full answer", out)

    def test_review_exit_status_as_command(self):
        res = run("review", "--handoff", HND, "--contract", CON,
                  "--helper-output", self.helper_file("pass"))
        self.assertEqual(res.returncode, 0)


class AcceptanceTests(Base):
    def test_acceptance_yes(self):
        res = self.acceptance()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "\n".join([
            "kind acceptance", "handoff " + HND, "contract " + CON, "review " + NOTE,
            "ACCEPTED yes | 2026-11-23"]) + "\n")

    def test_acceptance_no_with_reason(self):
        res = self.acceptance("--reason", "ACC-3 has no test", accepted="no")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.split("\n")[4], "ACCEPTED no | 2026-11-23 | ACC-3 has no test")

    def test_acceptance_yes_with_reason(self):
        res = self.acceptance("--reason", "owner said so")
        self.assertEqual(res.stdout.split("\n")[4], "ACCEPTED yes | 2026-11-23 | owner said so")

    def test_acceptance_with_two_rests_on(self):
        res = self.acceptance("--rests-on", "JUDGE PASS in NOTE-1", "--rests-on", "joined run exit 0")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.split("\n")[5:], [
            "rests-on JUDGE PASS in NOTE-1", "rests-on joined run exit 0", ""])

    def test_acceptance_without_rests_on_has_no_rests_on_line(self):
        self.assertNotIn("rests-on", self.acceptance().stdout)

    def test_acceptance_with_review_none(self):
        res = self.acceptance(review="none")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.split("\n")[3], "review none")

    def test_acceptance_with_note_id(self):
        res = self.acceptance(review="NOTE-20261123-12")
        self.assertEqual(res.stdout.split("\n")[3], "review NOTE-20261123-12")

    def test_acceptance_exit_status_as_command(self):
        self.assertEqual(self.acceptance().returncode, 0)


class CheckValidTests(Base):
    def assert_valid(self, text, kind):
        res, obj = self.check_text(text)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(res.stderr, "")
        self.assertEqual(obj, {"valid": True, "kind": kind, "findings": []})

    def test_check_review_note_plain_and_with_envelope(self):
        for kind in ("pass", "fail", "none", "off"):
            with self.subTest(kind):
                self.assert_valid(self.review(kind), "review")
                self.assert_valid(ENVELOPE + "\n" + self.review(kind), "review")

    def test_check_acceptance_notes_plain_and_with_envelope(self):
        notes = [self.acceptance().stdout,
                 self.acceptance("--reason", "r", accepted="no").stdout,
                 self.acceptance("--rests-on", "a", "--rests-on", "b").stdout,
                 self.acceptance(review="none").stdout]
        for note in notes:
            with self.subTest(note):
                self.assert_valid(note, "acceptance")
                self.assert_valid(ENVELOPE + "\n" + note, "acceptance")

    def test_check_skips_envelope_after_blank_lines(self):
        self.assert_valid("\n\n" + ENVELOPE + "\n" + self.review(), "review")

    def test_check_ignores_an_envelope_line_that_is_not_first(self):
        text = "kind review\n" + ENVELOPE + "\n" + "\n".join(self.review().split("\n")[1:])
        self.assert_valid(text, "review")

    def test_check_output_is_one_json_object_with_three_keys(self):
        res, obj = self.check_text(self.review())
        self.assertEqual(list(obj), ["valid", "kind", "findings"])
        self.assertEqual(res.stdout.count("\n"), 1)

    def test_check_exit_status_as_command(self):
        res = run("check", self.write("n.txt", self.review()))
        self.assertEqual(res.returncode, 0)
        res = run("check", self.write("m.txt", "kind other\n"))
        self.assertEqual(res.returncode, 1)


class DefectTests(Base):
    def rev(self):
        return self.review()

    def acc(self, **kw):
        return self.acceptance(*kw.pop("extra", ()), **kw).stdout

    def replace_line(self, text, key, new):
        lines = text.split("\n")
        idx = [i for i, l in enumerate(lines) if l.split(" ")[0] == key][0]
        if new is None:
            del lines[idx]
        else:
            lines[idx] = new
        return "\n".join(lines)

    def assert_finding(self, text, word):
        found = self.findings(text)
        self.assertTrue(any(word in f for f in found), found)

    def test_kind_neither_review_nor_acceptance(self):
        self.assert_finding(self.replace_line(self.rev(), "kind", "kind verdict"), "kind")

    def test_kind_missing(self):
        self.assert_finding(self.replace_line(self.rev(), "kind", None), "kind")

    def test_missing_key_of_review(self):
        for key in ("handoff", "contract", "JUDGE", "execution", "receipt"):
            with self.subTest(key):
                self.assert_finding(self.replace_line(self.rev(), key, None), key)

    def test_missing_key_of_acceptance(self):
        for key in ("handoff", "contract", "review", "ACCEPTED"):
            with self.subTest(key):
                self.assert_finding(self.replace_line(self.acc(), key, None), key)

    def test_key_standing_twice(self):
        for key, text in (("handoff", self.rev()), ("JUDGE", self.rev()), ("kind", self.rev()),
                          ("ACCEPTED", self.acc()), ("review", self.acc())):
            with self.subTest(key):
                line = [l for l in text.split("\n") if l.split(" ")[0] == key][0]
                self.assert_finding(text + line + "\n", key)

    def test_rests_on_may_stand_twice(self):
        res, obj = self.check_text(self.acc(extra=("--rests-on", "a", "--rests-on", "b")))
        self.assertEqual(res.returncode, 0)

    def test_handoff_value_no_hnd_id(self):
        for bad in ("CON-20261123-08", "HND-2026-17", "HND-20261123-1", "HND-20261123-1234567", "hnd-20261123-17", "x"):
            with self.subTest(bad):
                self.assert_finding(self.replace_line(self.rev(), "handoff", "handoff " + bad), "handoff")

    def test_contract_value_no_con_id(self):
        for bad in ("HND-20261123-17", "CON-20261123-1", "CON-2026112-08", "CON-20261123-08x", "x"):
            with self.subTest(bad):
                self.assert_finding(self.replace_line(self.rev(), "contract", "contract " + bad), "contract")

    def test_review_value_neither_none_nor_note_id(self):
        for bad in ("yes", "HND-20261123-17", "NOTE-20261123-1", "NOTE-20261123-1234567", "None"):
            with self.subTest(bad):
                self.assert_finding(self.replace_line(self.acc(), "review", "review " + bad), "review")

    def test_ids_accept_two_to_six_digit_numbers(self):
        for number in ("12", "123456"):
            text = self.replace_line(self.rev(), "handoff", "handoff HND-20261123-" + number)
            res, _ = self.check_text(text)
            self.assertEqual(res.returncode, 0, number)

    def test_judge_first_field_not_in_set(self):
        for bad in ("pass", "MAYBE", "yes"):
            with self.subTest(bad):
                self.assert_finding(self.replace_line(self.rev(), "JUDGE", "JUDGE      %s | a | b | c" % bad), "JUDGE")

    def test_judge_first_field_in_set(self):
        for word in ("PASS", "FAIL", "none", "off", "skipped"):
            with self.subTest(word):
                text = self.replace_line(self.rev(), "JUDGE", "JUDGE      %s | a | b | c" % word)
                res, _ = self.check_text(text)
                self.assertEqual(res.returncode, 0, word)

    def test_judge_fewer_than_four_fields(self):
        for bad in ("JUDGE      PASS | a | b", "JUDGE      PASS", "JUDGE      PASS | a"):
            with self.subTest(bad):
                self.assert_finding(self.replace_line(self.rev(), "JUDGE", bad), "JUDGE")

    def test_accepted_value_starts_with_neither_yes_nor_no(self):
        for bad in ("maybe | 2026-11-23", "YES | 2026-11-23", "| 2026-11-23", "yesterday | 2026-11-23",
                    "nope | 2026-11-23 | reason", "yes, | 2026-11-23", "nothing | 2026-11-23 | r"):
            with self.subTest(bad):
                self.assert_finding(self.replace_line(self.acc(), "ACCEPTED", "ACCEPTED " + bad), "ACCEPTED")

    def test_accepted_first_field_is_exactly_yes_or_no_after_trimming(self):
        for good in ("ACCEPTED yes | 2026-11-23", "ACCEPTED \tyes\t | 2026-11-23", "ACCEPTED no | 2026-11-23 | r"):
            with self.subTest(good):
                res, obj = self.check_text(self.replace_line(self.acc(), "ACCEPTED", good))
                self.assertEqual(res.returncode, 0, obj)

    def test_accepted_no_without_reason(self):
        for bad in ("ACCEPTED no | 2026-11-23", "ACCEPTED no | 2026-11-23 | ", "ACCEPTED no"):
            with self.subTest(bad):
                self.assert_finding(self.replace_line(self.acc(), "ACCEPTED", bad), "reason")

    def test_accepted_date_not_iso(self):
        for bad in ("23.11.2026", "2026-1-5", "2026-13-01", "2026-11-23T10:00", ""):
            with self.subTest(bad):
                self.assert_finding(self.replace_line(self.acc(), "ACCEPTED", "ACCEPTED yes | " + bad), "date")

    def test_check_reports_every_defect_not_only_the_first(self):
        text = "kind review\nhandoff x\ncontract y\n"
        self.assertGreaterEqual(len(self.findings(text)), 5)


class ExitTwoTests(Base):
    def assert_exit_two(self, *args):
        res = run(*args)
        self.assertEqual(res.returncode, 2, (args, res.stdout, res.stderr))
        self.assertEqual(res.stdout, "")
        self.assertEqual(len(res.stderr.strip().split("\n")), 1, res.stderr)
        self.assertTrue(res.stderr.strip())
        return res

    def rv(self, **over):
        opts = {"--handoff": HND, "--contract": CON, "--helper-output": self.helper_file("pass")}
        opts.update(over)
        args = ["review"]
        for k, v in opts.items():
            if v is not None:
                args += [k, v]
        return args

    def ac(self, **over):
        opts = {"--handoff": HND, "--contract": CON, "--review": NOTE, "--accepted": "yes", "--date": "2026-11-23"}
        opts.update(over)
        args = ["acceptance"]
        for k, v in opts.items():
            if v is not None:
                args += [k, v]
        return args

    def test_no_arguments_and_unknown_subcommand(self):
        self.assert_exit_two()
        self.assert_exit_two("verdict")

    def test_review_missing_each_option(self):
        for opt in ("--handoff", "--contract", "--helper-output"):
            with self.subTest(opt):
                self.assert_exit_two(*self.rv(**{opt: None}))

    def test_review_malformed_ids(self):
        self.assert_exit_two(*self.rv(**{"--handoff": "CON-20261123-08"}))
        self.assert_exit_two(*self.rv(**{"--contract": "HND-20261123-17"}))

    def test_review_unreadable_helper_file(self):
        self.assert_exit_two(*self.rv(**{"--helper-output": self.dir / "missing.txt"}))
        self.assert_exit_two(*self.rv(**{"--helper-output": self.dir}))

    def test_review_helper_output_malformed(self):
        short = self.write("short.txt", "JUDGE      PASS | a | b | c\nexecution  x\n")
        self.assert_exit_two(*self.rv(**{"--helper-output": short}))
        wrong = self.write("wrong.txt", "hello\nworld\nthere\n")
        self.assert_exit_two(*self.rv(**{"--helper-output": wrong}))
        empty = self.write("empty.txt", "")
        self.assert_exit_two(*self.rv(**{"--helper-output": empty}))

    def test_review_unknown_option(self):
        self.assert_exit_two(*(self.rv() + ["--verbose", "1"]))

    def test_acceptance_missing_each_option(self):
        for opt in ("--handoff", "--contract", "--review", "--accepted", "--date"):
            with self.subTest(opt):
                self.assert_exit_two(*self.ac(**{opt: None}))

    def test_acceptance_malformed_values(self):
        self.assert_exit_two(*self.ac(**{"--handoff": "x"}))
        self.assert_exit_two(*self.ac(**{"--contract": "x"}))
        self.assert_exit_two(*self.ac(**{"--review": "HND-20261123-17"}))
        self.assert_exit_two(*self.ac(**{"--review": "None"}))
        self.assert_exit_two(*self.ac(**{"--accepted": "maybe"}))
        self.assert_exit_two(*self.ac(**{"--date": "23.11.2026"}))
        self.assert_exit_two(*self.ac(**{"--date": "2026-02-30"}))

    def test_accepted_no_without_reason(self):
        self.assert_exit_two(*self.ac(**{"--accepted": "no"}))
        self.assert_exit_two(*(self.ac(**{"--accepted": "no"}) + ["--reason", ""]))
        self.assert_exit_two(*(self.ac(**{"--accepted": "no"}) + ["--rests-on", "x"]))

    def test_acceptance_text_with_line_break_is_refused(self):
        self.assert_exit_two(*(self.ac() + ["--reason", "a\nb"]))
        self.assert_exit_two(*(self.ac() + ["--rests-on", "a\nb"]))

    def test_check_missing_argument_and_unreadable_file(self):
        self.assert_exit_two("check")
        self.assert_exit_two("check", self.dir / "missing.txt")
        self.assert_exit_two("check", self.dir)

    def test_check_file_that_is_not_text(self):
        path = self.dir / "bin.dat"
        path.write_bytes(b"\xff\xfe\x00\x80")
        self.assert_exit_two("check", path)


if __name__ == "__main__":
    unittest.main()
