#!/usr/bin/env python3
"""Offline checks for the withdrawal note of scripts/closure_note.py (CON-20261123-09, ACC-1).

Run: python3 -m unittest discover -s checks -p 'test_withdrawal_note.py' -v
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "closure_note.py"

CON = "CON-20261123-31"
OTHER = "CON-20261123-32"
ENVELOPE = "<!-- readback record=NOTE-20261123-41 sha=0f0f -->"
GOOD = ["kind withdrawal", "contract " + CON, "replaced-by " + OTHER, "date 2026-11-23", "reason the scope moved"]

# One list of calls feeds the printing tests and the test that checks every printed note.
def argv(contract, replaced_by, date, reason):
    return ["withdrawal", "--contract", contract, "--replaced-by", replaced_by, "--date", date, "--reason", reason]


# Each entry is the full argument list of one call that exits 0. Every test that prints a withdrawal
# note with exit 0 takes its call from here by label, so the check of every printed note covers all.
CALLS = [
    ("with a CON id", argv(CON, OTHER, "2026-11-23", "the scope moved")),
    ("with none", argv(CON, "none", "2026-11-23", "dropped by the owner")),
    ("reason with a bar", argv(CON, "none", "2026-11-24", "a | b | c")),
    ("reason with inner spaces and a tab", argv(CON, "none", "2026-02-28", "two  words\tand a tab")),
    ("leap day", argv(CON, "none", "2028-02-29", "leap")),
    ("short and long ids", argv("CON-20261123-12", "CON-20261123-123456", "2026-11-23", "r")),
    ("reason with padding", argv(CON, "none", "2026-11-23", "   padded   ")),
    ("reason that starts with a key word", argv(CON, "none", "2026-11-23", "kind review")),
    ("one letter reason", argv(CON, "none", "2026-11-23", "x")),
    ("reason padded inside", argv(CON, "none", "2026-11-23", "   padded   inside  ")),
    ("options in another order", ["withdrawal", "--reason", "r", "--date", "2026-11-23", "--replaced-by", "none", "--contract", CON]),
]


def call(label):
    return run(*dict(CALLS)[label])


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT)] + [str(a) for a in args],
                          capture_output=True, text=True, timeout=60)


def withdraw(contract, replaced_by, date, reason):
    return run(*argv(contract, replaced_by, date, reason))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, text, name="note.txt"):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def check_text(self, text):
        res = run("check", self.write(text))
        return res, (json.loads(res.stdout) if res.stdout else None)

    def findings(self, lines, envelope=False):
        text = "\n".join(([ENVELOPE] if envelope else []) + lines) + "\n"
        res, obj = self.check_text(text)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertFalse(obj["valid"])
        self.assertEqual(obj["kind"], "withdrawal" if any(l.startswith("kind withdrawal") for l in lines) else obj["kind"])
        return obj["findings"]

    def with_line(self, key, value):
        return [("%s %s" % (key, value)) if l.split(" ")[0] == key else l for l in GOOD]


class PrintTests(Base):
    def test_exact_lines_with_a_con_id_in_replaced_by(self):
        res = call("with a CON id")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        self.assertEqual(res.stdout, "kind withdrawal\ncontract %s\nreplaced-by %s\ndate 2026-11-23\nreason the scope moved\n" % (CON, OTHER))

    def test_exact_lines_with_none_in_replaced_by(self):
        res = call("with none")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "kind withdrawal\ncontract %s\nreplaced-by none\ndate 2026-11-23\nreason dropped by the owner\n" % CON)

    def test_exactly_five_lines_in_the_order_of_the_contract(self):
        res = call("one letter reason")
        self.assertEqual([l.split(" ")[0] for l in res.stdout.split("\n")[:-1]],
                         ["kind", "contract", "replaced-by", "date", "reason"])

    def test_reason_is_trimmed_and_keeps_inner_text(self):
        res = call("reason padded inside")
        self.assertEqual(res.stdout.split("\n")[4], "reason padded   inside")

    def test_options_in_another_order(self):
        res = call("options in another order")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.split("\n")[1], "contract " + CON)


class CheckAcceptsTests(Base):
    def test_check_exit_0_for_the_plain_note(self):
        res, obj = self.check_text("\n".join(GOOD) + "\n")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(obj, {"valid": True, "kind": "withdrawal", "findings": []})

    def test_check_exit_0_for_the_note_with_an_envelope_line(self):
        res, obj = self.check_text("\n".join([ENVELOPE] + GOOD) + "\n")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(obj, {"valid": True, "kind": "withdrawal", "findings": []})

    def test_check_exit_0_with_none(self):
        res, obj = self.check_text("\n".join(self.with_line("replaced-by", "none")) + "\n")
        self.assertEqual(res.returncode, 0, res.stdout)

    def test_check_on_every_printed_note_plain_and_with_an_envelope(self):
        for label, args in CALLS:
            with self.subTest(label):
                res = run(*args)
                self.assertEqual(res.returncode, 0, res.stderr)
                for text in (res.stdout, ENVELOPE + "\n" + res.stdout):
                    checked, obj = self.check_text(text)
                    self.assertEqual(checked.returncode, 0, text + checked.stdout)
                    self.assertEqual(obj["findings"], [])
                    self.assertEqual(obj["kind"], "withdrawal")


class DefectTests(Base):
    def assert_finding(self, lines, word, envelope=False):
        found = self.findings(lines, envelope)
        self.assertTrue(any(word in f for f in found), found)

    def test_missing_key_for_each_key(self):
        for key in ("contract", "replaced-by", "date", "reason"):
            with self.subTest(key):
                lines = [l for l in GOOD if l.split(" ")[0] != key]
                self.assertEqual(self.findings(lines), ["missing key %s" % key])

    def test_missing_kind_is_reported(self):
        self.assertIn("missing key kind", self.findings(GOOD[1:]))

    def test_a_key_that_stands_twice_for_each_key(self):
        for key in ("kind", "contract", "replaced-by", "date", "reason"):
            with self.subTest(key):
                line = [l for l in GOOD if l.split(" ")[0] == key][0]
                self.assertEqual(self.findings(GOOD + [line]), ["key %s stands twice" % key])

    def test_a_key_that_stands_twice_with_another_value(self):
        self.assertEqual(self.findings(GOOD + ["date 2026-11-24"]), ["key date stands twice"])

    def test_contract_value_that_is_no_con_id(self):
        for value in ("HND-20261123-31", "CON-2026112-31", "CON-20261123-1", "CON-20261123-1234567", "con-20261123-31",
                      "CON-20261123-31x", "none", "CON-20261123-31 CON-20261123-32", "CON-٢٠٢٦١١٢٣-31"):
            with self.subTest(value):
                self.assertEqual(self.findings(self.with_line("contract", value)),
                                 ["contract value is no CON id: %r" % value])

    def test_replaced_by_that_is_neither_none_nor_a_con_id(self):
        for value in ("NOTE-20261123-31", "None", "CON-20261123-1", "CON-20261123-32 x", "-", "HND-20261123-31"):
            with self.subTest(value):
                self.assertEqual(self.findings(self.with_line("replaced-by", value)),
                                 ["replaced-by value is neither none nor a CON id: %r" % value])

    def test_replaced_by_equal_to_contract(self):
        self.assertEqual(self.findings(self.with_line("replaced-by", CON)), ["replaced-by equals contract"])

    def test_date_that_is_not_yyyy_mm_dd(self):
        for value in ("2026-13-01", "2026-02-30", "2026-1-01", "23-11-2026", "2026/11/23", "2026-11-23T10:00", "tomorrow",
                      "٢٠٢٦-11-23", "2027-02-29"):
            with self.subTest(value):
                found = self.findings(self.with_line("date", value))
                self.assertEqual(found, ["date is not YYYY-MM-DD: %r" % value])

    def test_empty_reason(self):
        for line in ("reason ", "reason \t ", "reason "):
            with self.subTest(line):
                lines = GOOD[:4] + [line]
                found = self.findings(lines)
                self.assertTrue(found == ["reason is empty"] or found == ["missing key reason"], found)

    def test_empty_reason_with_a_trailing_space_reports_empty(self):
        self.assertEqual(self.findings(GOOD[:4] + ["reason "]), ["reason is empty"])

    def test_each_defect_with_an_envelope_line(self):
        self.assertEqual(self.findings(self.with_line("replaced-by", CON), envelope=True), ["replaced-by equals contract"])
        self.assertEqual(self.findings(GOOD[:4], envelope=True), ["missing key reason"])

    def test_check_exit_1_and_findings_for_two_defects(self):
        lines = self.with_line("date", "soon")
        lines = [("contract HND-20261123-1") if l.startswith("contract") else l for l in lines]
        self.assertEqual(len(self.findings(lines)), 2)

    def test_other_kinds_have_no_new_findings(self):
        # the withdrawal rules apply to kind withdrawal only
        text = "\n".join(["kind acceptance", "handoff HND-20261123-17", "contract " + CON, "review none",
                          "ACCEPTED yes | 2026-11-23", "reason ", "date soon", "replaced-by " + CON]) + "\n"
        res, obj = self.check_text(text)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(obj["findings"], [])

    def test_an_unknown_kind_is_a_defect_that_names_withdrawal(self):
        found = self.findings(["kind withdrawn"] + GOOD[1:])
        self.assertEqual(found, ["kind is none of review, acceptance and withdrawal: 'withdrawn'"])


class ExitTwoTests(Base):
    def assert_refused(self, res, word=None):
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertEqual(res.stdout, "")
        self.assertEqual(len(res.stderr.rstrip("\n").split("\n")), 1, res.stderr)
        self.assertTrue(res.stderr.endswith("\n"))
        if word:
            self.assertIn(word, res.stderr)

    def base(self, **over):
        values = {"--contract": CON, "--replaced-by": OTHER, "--date": "2026-11-23", "--reason": "r"}
        values.update(over)
        args = ["withdrawal"]
        for key, value in values.items():
            if value is not None:
                args += [key, value]
        return run(*args)

    def test_each_missing_option(self):
        for option in ("--contract", "--replaced-by", "--date", "--reason"):
            with self.subTest(option):
                self.assert_refused(self.base(**{option: None}))

    def test_no_options(self):
        self.assert_refused(run("withdrawal"))

    def test_contract_that_is_no_con_id(self):
        for value in ("HND-20261123-31", "CON-20261123-1", "x", "", "CON-20261123-31\n", "CON-٢٠٢٦١١٢٣-31"):
            with self.subTest(value):
                self.assert_refused(self.base(**{"--contract": value}), "--contract")

    def test_replaced_by_that_is_neither_none_nor_a_con_id(self):
        for value in ("NOTE-20261123-31", "None", "", "CON-20261123-1"):
            with self.subTest(value):
                self.assert_refused(self.base(**{"--replaced-by": value}), "--replaced-by")

    def test_replaced_by_equal_to_contract(self):
        self.assert_refused(self.base(**{"--replaced-by": CON}), "--replaced-by")

    def test_date_that_is_not_a_date(self):
        for value in ("2026-02-30", "2026-1-1", "", "2026-11-23\n", "now", "٢٠٢٦-11-23"):
            with self.subTest(value):
                self.assert_refused(self.base(**{"--date": value}), "--date")

    def test_empty_reason(self):
        for value in ("", "   ", "\t", " "):
            with self.subTest(repr(value)):
                self.assert_refused(self.base(**{"--reason": value}), "--reason")

    def test_reason_with_a_line_break(self):
        for value in ("a\nb", "a\rb", "a\r\nb"):
            with self.subTest(repr(value)):
                self.assert_refused(self.base(**{"--reason": value}), "--reason")

    def test_unknown_option_and_stray_argument(self):
        self.assert_refused(run("withdrawal", "--contract", CON, "--replaced-by", "none", "--date", "2026-11-23",
                                "--reason", "r", "--extra", "x"))
        self.assert_refused(run("withdrawal", "--contract", CON, "--replaced-by", "none", "--date", "2026-11-23",
                                "--reason", "r", "stray"))

    def test_option_without_a_value(self):
        self.assert_refused(run("withdrawal", "--contract", CON, "--replaced-by", "none", "--date", "2026-11-23", "--reason"))

    def test_abbreviated_option_is_refused(self):
        self.assert_refused(run("withdrawal", "--contr", CON, "--replaced-by", "none", "--date", "2026-11-23", "--reason", "r"))

    def test_the_first_argument_is_withdrawal_review_acceptance_or_check(self):
        self.assert_refused(run("withdraw", "--contract", CON), "withdrawal")

    def test_the_script_writes_no_file(self):
        before = sorted(p.name for p in (ROOT / "scripts").iterdir())
        call("with none")
        self.base(**{"--date": "bad"})
        self.assertEqual(sorted(p.name for p in (ROOT / "scripts").iterdir()), before)


class SelfCheckTests(Base):
    """The script prints no note that its own check rejects (a rule of the Definitions)."""

    def test_check_accepts_every_note_that_exits_zero(self):
        for label, args in CALLS:
            with self.subTest(label):
                res = run(*args)
                self.assertEqual(res.returncode, 0, res.stderr)
                checked, obj = self.check_text(res.stdout)
                self.assertEqual(checked.returncode, 0, res.stdout)

    def test_every_call_that_exits_two_prints_nothing(self):
        bad = [(CON, CON, "2026-11-23", "r"), ("HND-20261123-1", "none", "2026-11-23", "r"),
               (CON, "none", "2026-13-40", "r"), (CON, "none", "2026-11-23", " "), (CON, "x", "2026-11-23", "r")]
        for args in bad:
            with self.subTest(args):
                res = withdraw(*args)
                self.assertEqual((res.returncode, res.stdout), (2, ""))

    def test_the_self_check_runs_the_rules_of_check(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        sys.dont_write_bytecode = True
        try:
            import closure_note
        finally:
            sys.path.remove(str(ROOT / "scripts"))
        self.assertTrue(hasattr(closure_note, "self_check"))
        with self.assertRaises(closure_note.Refuse):
            closure_note.self_check(["kind withdrawal", "contract " + CON, "replaced-by " + CON, "date 2026-11-23", "reason r"])
        self.assertEqual(closure_note.self_check(GOOD), "\n".join(GOOD) + "\n")


if __name__ == "__main__":
    unittest.main()
