#!/usr/bin/env python3
"""Offline checks for scripts/validate_handoff.py.

Run one class: python3 -B checks/test_validate_handoff.py GrammarTests
Run all: python3 -m unittest discover -s checks -p 'test_validate_handoff.py'
"""

import contextlib
import importlib.util
import hashlib
import io
import json
import os
import random
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_handoff.py"

_spec = importlib.util.spec_from_file_location("validate_handoff_under_test", SCRIPT)
vh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vh)

CONTRACT = (
    "CON-20261117-01  Do the thing.\n"
    "ACC-1  The tool shall work.   Check: sh a.sh\n"
    "ACC-2  The tool shall pass.   Check: sh b.sh\n"
    "Out of scope: everything else\n"
)


def handoff(state="DONE", contract="ACC-1 VERIFIED | ACC-2 VERIFIED",
            checks=("sh a.sh | exit 0 | ok", "sh b.sh | exit 0 | ok"), drop=(), extra=""):
    rows = [
        ("id", "HND-20261117-01"), ("from", "worker"), ("date", "2026-11-17"),
        ("state", state), ("readback", "task: x | limits: y | done when: ACC-1, ACC-2"),
        ("summary", "Done."), ("changes", "none"), ("commit", "none"),
        ("checks", None), ("contract", contract), ("open", "-"), ("issues", "-"),
        ("next", "Accept."),
    ]
    lines = []
    for key, value in rows:
        if key in drop:
            continue
        if key == "checks":
            lines.append("checks    " + checks[0])
            lines.extend("          " + c for c in checks[1:])
        else:
            lines.append("%-9s %s" % (key, value))
    return "\n".join(lines) + "\n" + extra


def rules(result):
    return [(f["rule"], f["acc"], f["level"]) for f in result["findings"]]


class GrammarTests(unittest.TestCase):
    def test_text_handoff_has_keys_in_order_and_continuation(self):
        parsed = vh.parse_handoff(handoff())
        self.assertTrue(parsed["ok"])
        self.assertEqual([k for k, _ in parsed["pairs"]], list(vh.REQUIRED_KEYS))
        self.assertEqual(parsed["values"]["checks"], "sh a.sh | exit 0 | ok\nsh b.sh | exit 0 | ok")

    def test_text_contract_is_parsed(self):
        parsed = vh.parse_contract(CONTRACT)
        self.assertEqual(parsed["id"], "CON-20261117-01")
        self.assertEqual([s["id"] for s in parsed["statements"]], ["ACC-1", "ACC-2"])
        self.assertEqual(parsed["statements"][1]["check"], "sh b.sh")
        self.assertEqual(parsed["statements"][0]["statement"], "The tool shall work.")
        self.assertEqual(parsed["out_of_scope"], "everything else")

    def test_contract_continuation_joins_with_one_space(self):
        parsed = vh.parse_contract("CON-20261117-01  T\nACC-1  The x shall y.\n   Check: sh a.sh\n")
        self.assertEqual(parsed["statements"][0]["check"], "sh a.sh")

    def test_json_handoff_matches_text_handoff(self):
        text = handoff()
        obj = {k: v for k, v in vh.parse_handoff(text)["values"].items()}
        parsed = vh.parse_handoff(json.dumps(obj))
        self.assertEqual(parsed["form"], "json")
        self.assertEqual(parsed["values"], vh.parse_handoff(text)["values"])
        self.assertEqual(vh.validate(CONTRACT, json.dumps(obj)), vh.validate(CONTRACT, text))

    def test_json_handoff_accepts_lists_and_a_contract_object(self):
        obj = json.loads(json.dumps({k: v for k, v in vh.parse_handoff(handoff())["values"].items()}))
        obj["checks"] = ["sh a.sh | exit 0 | ok", "sh b.sh | exit 0 | ok"]
        obj["contract"] = {"ACC-1": "VERIFIED", "ACC-2": "VERIFIED"}
        self.assertEqual(vh.validate(CONTRACT, json.dumps(obj))["valid"], True)

    def test_json_contract_forms(self):
        as_keys = {"id": "CON-20261117-01", "task": "t",
                   "ACC-1": "The tool shall work.   Check: sh a.sh",
                   "ACC-2": {"statement": "The tool shall pass.", "check": "sh b.sh"}}
        as_list = {"id": "CON-20261117-01", "task": "t", "acc": [
            {"id": "ACC-1", "statement": "s", "check": "sh a.sh"},
            {"id": "ACC-2", "statement": "s", "check": "sh b.sh"}]}
        for obj in (as_keys, as_list):
            parsed = vh.parse_contract(json.dumps(obj))
            self.assertEqual(parsed["form"], "json")
            self.assertEqual([s["check"] for s in parsed["statements"]], ["sh a.sh", "sh b.sh"])
            self.assertEqual(vh.validate(json.dumps(obj), handoff())["valid"], True)

    def test_extra_keys_are_kept_and_are_not_a_defect(self):
        text = handoff(extra="profile    extra value\n          more\n")
        parsed = vh.parse_handoff(text)
        self.assertEqual(parsed["extra"], {"profile": "extra value\nmore"})
        contract = CONTRACT + "Note  an extra line\n"
        self.assertEqual(vh.parse_contract(contract)["extra"], {"Note": "an extra line"})
        result = vh.validate(contract, text)
        self.assertEqual(result["valid"], True)
        obj = {k: v for k, v in vh.parse_handoff(handoff())["values"].items()}
        obj["profile"] = "x"
        self.assertEqual(vh.parse_handoff(json.dumps(obj))["extra"], {"profile": "x"})
        self.assertEqual(vh.validate(CONTRACT, json.dumps(obj))["valid"], True)

    def test_unreadable_input_is_unresolved_not_defect(self):
        for bad_handoff in ("{not json", "", "   indented first\n"):
            result = vh.validate(CONTRACT, bad_handoff)
            self.assertEqual(result["valid"], "unparsed", bad_handoff)
            self.assertTrue(all(f["level"] == "unresolved" for f in result["findings"]))
        result = vh.validate("[1]", handoff())
        self.assertEqual(result["valid"], "unparsed")

    def test_line_that_is_not_a_key_is_unresolved(self):
        result = vh.validate(CONTRACT, handoff(extra="!! odd line\n"))
        self.assertEqual(result["valid"], "unparsed")
        self.assertEqual(rules(result), [("V2", None, "unresolved")])


class StructureTests(unittest.TestCase):
    def test_sound_pair_is_valid(self):
        self.assertEqual(vh.validate(CONTRACT, handoff()), {
            "valid": True, "findings": [], "validator_version": vh.VALIDATOR_VERSION})

    def test_v1_contract_id(self):
        for contract in (CONTRACT.replace("CON-20261117-01", "CON-2026-1"),
                         CONTRACT.replace("CON-20261117-01  Do the thing.\n", "")):
            self.assertIn(("V1", None, "defect"), rules(vh.validate(contract, handoff())))

    def test_v1_statement_count(self):
        none = "CON-20261117-01  T\nOut of scope: x\n"
        six = "CON-20261117-01  T\n" + "".join("ACC-%d  s   Check: c%d\n" % (i, i) for i in range(1, 7))
        self.assertIn(("V1", None, "defect"), rules(vh.validate(none, handoff())))
        self.assertIn(("V1", None, "defect"), rules(vh.validate(six, handoff())))
        five = "CON-20261117-01  T\n" + "".join("ACC-%d  s   Check: c%d\n" % (i, i) for i in range(1, 6))
        self.assertNotIn(("V1", None, "defect"), rules(vh.validate(five, handoff())))

    def test_v1_missing_check_and_repeated_acc(self):
        contract = "CON-20261117-01  T\nACC-1  s\nACC-2  s   Check: sh b.sh\nACC-2  s   Check: sh b.sh\n"
        found = rules(vh.validate(contract, handoff(contract="ACC-1 UNVERIFIED | ACC-2 UNVERIFIED",
                                                   state="PARTIAL")))
        self.assertIn(("V1", "ACC-1", "defect"), found)
        self.assertIn(("V1", "ACC-2", "defect"), found)

    def test_v2_missing_key(self):
        result = vh.validate(CONTRACT, handoff(drop=("next",)))
        self.assertEqual(rules(result), [("V2", None, "defect")])
        self.assertFalse(result["valid"])

    def test_v2_repeated_key_and_order(self):
        repeated = handoff(extra="next      again\n")
        self.assertIn(("V2", None, "defect"), rules(vh.validate(CONTRACT, repeated)))
        lines = handoff().splitlines()
        lines[0], lines[1] = lines[1], lines[0]
        self.assertIn(("V2", None, "defect"), rules(vh.validate(CONTRACT, "\n".join(lines) + "\n")))

    def test_v2_state_value(self):
        result = vh.validate(CONTRACT, handoff(state="OK"))
        self.assertIn(("V2", None, "defect"), rules(result))

    def test_v3_missing_repeated_unknown_acc(self):
        missing = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 VERIFIED"))
        self.assertEqual(rules(missing), [("V3", "ACC-2", "defect")])
        repeated = vh.validate(CONTRACT, handoff(contract="ACC-1 VERIFIED | ACC-1 VERIFIED | ACC-2 VERIFIED"))
        self.assertEqual(rules(repeated), [("V3", "ACC-1", "defect")])
        unknown = vh.validate(CONTRACT, handoff(contract="ACC-1 VERIFIED | ACC-2 VERIFIED | ACC-3 VERIFIED"))
        self.assertEqual(rules(unknown), [("V3", "ACC-3", "defect")])

    def test_v3_unknown_status_word_and_reason(self):
        result = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 PASSED | ACC-2 UNVERIFIED (not run)"))
        self.assertEqual(rules(result), [("V3", "ACC-1", "defect")])
        ok = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 UNVERIFIED (a) | ACC-2 FAILED (b)",
                                          checks=("sh b.sh | exit 1 | bad",)))
        self.assertEqual(ok["valid"], True)

    def test_v3_unreadable_part_is_unresolved(self):
        result = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 UNVERIFIED because x"))
        self.assertEqual(result["valid"], "unparsed")
        self.assertTrue(all(f["level"] == "unresolved" for f in result["findings"]))

    def test_v5_done_needs_every_acc_verified(self):
        result = vh.validate(CONTRACT, handoff(contract="ACC-1 VERIFIED | ACC-2 UNVERIFIED (not run)"))
        self.assertEqual(rules(result), [("V5", "ACC-2", "defect")])
        partial = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 VERIFIED | ACC-2 UNVERIFIED (x)"))
        self.assertEqual(partial["valid"], True)


class EvidenceTests(unittest.TestCase):
    def test_v4_command_without_exit_code_or_result(self):
        # the line command equals the Check: text, so the missing part is a certain defect
        for line in ("sh b.sh | (output cut)", "sh b.sh | exit 0", "sh b.sh"):
            result = vh.validate(CONTRACT, handoff(checks=("sh a.sh | exit 0 | ok", line)))
            self.assertEqual(rules(result), [("V4", "ACC-2", "defect")], line)

    def test_result_is_taken_by_position_not_by_words(self):
        # literal output that looks like a marker must not give a defect
        cases = [
            ('printf "no exit code found"', 'printf "no exit code found" | exit 0 | no exit code found'),
            ("printf none", "printf none | exit 0 | none"),
            ("printf tool", "printf tool | exit 0 | tool call"),
            ("printf x", "printf x | exit 0 | (output cut)"),
            ("printf x", "printf x | exit 0 | truncated"),
            ("printf a", "printf a | exit 0 | a | b"),
        ]
        for check, line in cases:
            contract = "CON-20261117-01  T\nACC-1  s   Check: %s\n" % check
            result = vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=(line,)))
            self.assertNotEqual(result["valid"], False, line)
            self.assertNotIn("defect", [f["level"] for f in result["findings"]], line)
        # plain results are complete evidence
        for check, line in [("printf a", "printf a | exit 0 | a | b"), ("printf ok", "printf ok | exit 0 | ok")]:
            contract = "CON-20261117-01  T\nACC-1  s   Check: %s\n" % check
            self.assertEqual(vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=(line,)))["valid"], True)

    def test_marker_like_result_after_a_real_exit_code_is_unresolved(self):
        contract = "CON-20261117-01  T\nACC-1  s   Check: printf x\n"
        result = vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=("printf x | exit 0 | (output cut)",)))
        self.assertEqual(rules(result), [("V4", "ACC-1", "unresolved")])
        self.assertEqual(result["valid"], "unparsed")

    def test_exit_text_as_output_never_gives_a_defect(self):
        # two exit-code segments: the line is not read, so it is doubtful and proves no defect
        contract = "CON-20261117-01  T\nACC-1  s   Check: printf 'exit 0'\n"
        result = vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=("printf 'exit 0' | exit 0 | exit 0",)))
        self.assertEqual(rules(result), [("V6", "ACC-1", "unresolved")])

    def test_absent_result_after_exit_code_is_a_defect(self):
        contract = "CON-20261117-01  T\nACC-1  s   Check: printf x\n"
        result = vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=("printf x | exit 0",)))
        self.assertEqual(rules(result), [("V4", "ACC-1", "defect")])

    def test_v4_doubtful_command_is_only_unresolved(self):
        # "output ok" may be the result or part of the command: no defect without certainty
        for line in ("sh b.sh | output ok", "sh b.sh | x | y", "sh b.sh | exit 0 | exit 1 | ok"):
            result = vh.validate(CONTRACT, handoff(checks=("sh a.sh | exit 0 | ok", line)))
            self.assertEqual(rules(result), [("V6", "ACC-2", "unresolved")], line)

    def test_v4_failed_status_needs_evidence_too(self):
        result = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 VERIFIED | ACC-2 FAILED (x)",
                                              checks=("sh a.sh | exit 0 | ok", "sh b.sh | exit 1")))
        self.assertEqual(rules(result), [("V4", "ACC-2", "defect")])

    def test_v4_no_evidence_at_all(self):
        result = vh.validate(CONTRACT, handoff(checks=("none",)))
        self.assertIn(("V4", "ACC-1", "defect"), rules(result))
        self.assertIn(("V4", "ACC-2", "defect"), rules(result))

    def test_unverified_status_needs_no_evidence(self):
        result = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 VERIFIED | ACC-2 UNVERIFIED (x)",
                                              checks=("sh a.sh | exit 0 | ok",)))
        self.assertEqual(result["valid"], True)

    def test_tool_call_needs_no_exit_code_but_needs_a_result(self):
        contract = "CON-20261117-04  T\nACC-1  s   Check: Grep tool, pattern 'x'\n"
        good = "Grep tool, pattern 'x' | no exit code (tool call) | one match"
        bad = "Grep tool, pattern 'x' | no exit code (tool call)"
        self.assertEqual(vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=(good,)))["valid"], True)
        self.assertEqual(rules(vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=(bad,)))),
                         [("V4", "ACC-1", "defect")])

    def test_shell_command_is_not_taken_for_a_tool_call(self):
        # "no exit code" without "tool call" does not make a tool call; the line cannot be tied: V6 only
        result = vh.validate(CONTRACT, handoff(checks=("sh a.sh | exit 0 | ok", "sh b.sh | no exit code | ok")))
        self.assertEqual(rules(result), [("V6", "ACC-2", "unresolved")])

    def test_v6_text_mismatch_is_unresolved_never_defect(self):
        result = vh.validate(CONTRACT, handoff(checks=("sh a.sh | exit 0 | ok", "sh b.sh --fast | exit 0 | ok")))
        self.assertEqual(rules(result), [("V6", "ACC-2", "unresolved")])
        self.assertEqual(result["valid"], "unparsed")

    def test_v6_does_not_guess_equivalence(self):
        for line in ("bash b.sh | exit 0 | ok", "sh ./b.sh | exit 0 | ok", "sh  b.sh  | exit 0 | ok"):
            result = vh.validate(CONTRACT, handoff(checks=("sh a.sh | exit 0 | ok", line)))
            self.assertEqual(rules(result), [("V6", "ACC-2", "unresolved")], line)

    def test_v6_whole_check_with_semicolon_matches_one_line(self):
        contract = "CON-20261117-01  T\nACC-1  s   Check: cd x; make\n"
        result = vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=("cd x; make | exit 0 | ok",)))
        self.assertEqual(result["valid"], True)

    def test_v6_applies_to_verified_only(self):
        result = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 VERIFIED | ACC-2 FAILED (x)",
                                              checks=("sh a.sh | exit 0 | ok", "other | exit 1 | bad")))
        self.assertNotIn("V6", [r[0] for r in rules(result)])

    def test_v6_exact_text_inside_the_command(self):
        # whitespace, case and quotes inside the command are never folded
        cases = [
            ('printf "a  b"', 'printf "a b"'),
            ("printf 'a b'", 'printf "a b"'),
            ("sh a.sh", "sh\ta.sh"),
            ("sh a.sh", "SH a.sh"),
            ("sh a.sh", "sh a.sh."),
            ("sh a.sh", "sh a.sh  --x"),
        ]
        for check, line in cases:
            contract = "CON-20261117-01  T\nACC-1  s   Check: %s\n" % check
            result = vh.validate(contract, handoff(contract="ACC-1 VERIFIED",
                                                  checks=(line + " | exit 0 | a b",)))
            self.assertEqual(rules(result), [("V6", "ACC-1", "unresolved")], (check, line))
            self.assertEqual(result["valid"], "unparsed")

    def test_v6_semicolon_is_not_split(self):
        contract = "CON-20261117-01  T\nACC-1  s   Check: sh a.sh; sh b.sh\n"
        two_lines = handoff(contract="ACC-1 VERIFIED", checks=("sh a.sh | exit 0 | ok", "sh b.sh | exit 0 | ok"))
        self.assertEqual(rules(vh.validate(contract, two_lines)), [("V6", "ACC-1", "unresolved")])
        whole = handoff(contract="ACC-1 VERIFIED", checks=("sh a.sh; sh b.sh | exit 0 | ok",))
        self.assertEqual(vh.validate(contract, whole)["valid"], True)
        cut_line = handoff(contract="ACC-1 VERIFIED", checks=("sh a.sh | exit 0 | ok", "sh a.sh; sh b.sh | (output cut)"))
        self.assertEqual(rules(vh.validate(contract, cut_line)), [("V4", "ACC-1", "defect")])

    def test_v6_only_ascii_space_and_tab_are_removed_at_the_ends(self):
        contract = "CON-20261117-01  T\nACC-1  s   Check:  \t printf \"a  b\" \t \n"
        ok = vh.validate(contract, handoff(contract="ACC-1 VERIFIED",
                                          checks=('  \t printf "a  b" \t | exit 0 | a  b',)))
        self.assertEqual(ok["valid"], True)
        for line in ("`printf \"a  b\"`", "\u00a0printf \"a  b\"", 'printf "a  b"\u00a0'):
            bad = vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=(line + " | exit 0 | x",)))
            self.assertEqual(rules(bad), [("V6", "ACC-1", "unresolved")], line)

    def test_round_1_case_whitespace_inside_quotes(self):
        contract = 'CON-20261117-01  T\nACC-1  s   Check: printf "a  b"\n'
        result = vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=('printf "a b" | exit 0 | a b',)))
        self.assertEqual(rules(result), [("V6", "ACC-1", "unresolved")])
        self.assertEqual(result["valid"], "unparsed")

    def test_round_2_case_continuation_line_never_matches(self):
        contract = 'CON-20261117-01  T\nACC-1  s   Check: printf "a\n    b"\n'
        result = vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=('printf "a b" | exit 0 | a b',)))
        self.assertEqual(rules(result), [("V6", "ACC-1", "unresolved")])
        self.assertEqual(result["valid"], "unparsed")
        # even a line that reads exactly like the first physical line does not match
        again = vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=('printf "a | exit 0 | a',)))
        self.assertEqual(rules(again), [("V6", "ACC-1", "unresolved")])

    def test_json_newlines_never_match(self):
        contract = json.dumps({"id": "CON-20261117-01", "ACC-1": {"statement": "s", "check": "a\nb"}})
        keys = vh.parse_handoff(handoff())["values"]
        for checks in (["a b | exit 0 | ok"], ["a\nb | exit 0 | ok"]):
            obj = dict(keys, contract="ACC-1 VERIFIED", checks=checks)
            self.assertEqual(rules(vh.validate(contract, json.dumps(obj))), [("V6", "ACC-1", "unresolved")])
        contract = json.dumps({"id": "CON-20261117-01", "ACC-1": {"statement": "s", "check": "a b"}})
        obj = dict(keys, contract="ACC-1 VERIFIED", checks=["a\nb | exit 0 | ok", "a b | exit 0"])
        # the newline item is not read; the other item equals the check and lacks a result
        self.assertEqual(rules(vh.validate(contract, json.dumps(obj))), [("V4", "ACC-1", "defect")])

    def test_command_with_a_bar_matches_whole_and_first_segment_alone_does_not(self):
        line = "sh a.sh | tee out | exit 0 | ok"
        whole = "CON-20261117-01  T\nACC-1  s   Check: sh a.sh | tee out\n"
        first = "CON-20261117-01  T\nACC-1  s   Check: sh a.sh\n"
        text = handoff(contract="ACC-1 VERIFIED", checks=(line,))
        self.assertEqual(vh.validate(whole, text)["valid"], True)
        self.assertEqual(rules(vh.validate(first, text)), [("V6", "ACC-1", "unresolved")])
        short = handoff(contract="ACC-1 VERIFIED", checks=("sh a.sh | exit 0 | ok",))
        self.assertEqual(rules(vh.validate(whole, short)), [("V6", "ACC-1", "unresolved")])

    def test_crlf_input_is_unresolved_not_defect(self):
        # a trailing carriage return is part of the raw text, so it never equals the command
        result = vh.validate(CONTRACT.replace("\n", "\r\n"), handoff().replace("\n", "\r\n"))
        self.assertEqual(result["valid"], "unparsed")
        self.assertTrue(all(f["level"] == "unresolved" and f["rule"] == "V6" for f in result["findings"]))

    ALPHABET = "abAB \t\n|;'\"`\u00a0"

    @classmethod
    def mutate(cls, rng, text):
        kind = rng.randrange(9)
        i = rng.randrange(len(text) + 1)
        if kind == 0 and text:
            return text.swapcase()
        if kind == 1:
            return text.replace(" ", "\t", 1)
        if kind == 2:
            return text[:i] + "\n" + text[i:]
        if kind == 3:
            return text[:i] + " " + text[i:]
        if kind == 4:
            return "`" + text + "`"
        if kind == 5:
            return text + ";x"
        if kind == 6:
            return "\u00a0" + text
        if kind == 7:
            return text.replace('"', "'", 1)
        return text + "\u00a0"

    def generate(self, rng):
        base = "".join(rng.choice(self.ALPHABET) for _ in range(rng.randrange(1, 9)))
        if not base.strip(" \t"):
            base += "x"
        check = base if rng.random() < 0.3 else self.mutate(rng, base)
        if not check.strip(" \t"):
            check += "x"
        lines = []
        for _ in range(rng.randrange(1, 4)):
            cmd = base if rng.random() < 0.6 else "".join(rng.choice(self.ALPHABET) for _ in range(rng.randrange(1, 6)))
            if rng.random() < 0.3:
                cmd = self.mutate(rng, cmd)
            if not cmd.strip(" \t"):
                cmd += "y"
            form = rng.choice("ABCD")
            result_text = rng.choice(["ok", "ok", "none", "(output cut)", "exit 0", "tool call",
                                      "no exit code found", "cut", "truncated", "a | b"])
            suffix = {"A": " | exit 0 | " + result_text, "B": " | (output cut)", "C": " | exit 0", "D": ""}[form]
            lines.append((form, cmd, cmd + suffix))
        return check, lines

    @staticmethod
    def same(check, cmd):
        # written from rule 1 and not from the validator's helpers
        return "\n" not in check and "\n" not in cmd and check.strip(" \t") == cmd.strip(" \t")

    def run_property(self, rng, cases, as_text):
        keys = vh.parse_handoff(handoff())["values"]
        seen = {"generated": cases, "evaluated": 0, "no_v6": 0, "v6": 0, "v4_defect": 0}
        for n in range(cases):
            check, lines = self.generate(rng)
            texts = [l[2] for l in lines]
            if as_text:
                if any("\n" in x for x in texts + [check]):
                    continue
            seen["evaluated"] += 1
            if as_text:
                contract = "CON-20261117-01  T\nACC-1  s   Check: " + check + "\n"
                text = handoff(state="DONE", contract="ACC-1 VERIFIED", checks=tuple(texts))
            else:
                contract = json.dumps({"id": "CON-20261117-01", "ACC-1": {"statement": "s", "check": check}})
                text = json.dumps(dict(keys, state="DONE", contract="ACC-1 VERIFIED", checks=texts))
            result = vh.validate(contract, text)
            where = (n, check, lines)
            for f in result["findings"]:
                self.assertIn(f["rule"], ("V4", "V6"), where)
            has_v6 = any(f["rule"] == "V6" for f in result["findings"])
            # P1: no V6 finding means the raw check equals the command of some line
            if not has_v6:
                self.assertTrue(any(self.same(check, cmd) for _, cmd, _ in lines), where)
                seen["no_v6"] += 1
            else:
                seen["v6"] += 1
            # P2: a V4 defect needs no complete line, or an equal line that lacks exit code or result
            if any(f["rule"] == "V4" and f["level"] == "defect" for f in result["findings"]):
                none_complete = not any(form == "A" for form, _, _ in lines)
                equal_incomplete = any(form != "A" and self.same(check, cmd) for form, cmd, _ in lines)
                self.assertTrue(none_complete or equal_incomplete, where)
                seen["v4_defect"] += 1
        return seen

    def test_property_json_form_2500_generated_cases(self):
        seen = self.run_property(random.Random(20261117), 2500, as_text=False)
        self.assertEqual((seen["generated"], seen["evaluated"]), (2500, 2500))
        self.assertGreater(seen["no_v6"], 100)
        self.assertGreater(seen["v6"], 100)
        self.assertGreater(seen["v4_defect"], 50)

    def test_property_text_form_1500_generated_cases(self):
        # cases whose text holds a newline cannot be written in the text form and are skipped
        seen = self.run_property(random.Random(20261118), 1500, as_text=True)
        self.assertEqual(seen["generated"], 1500)
        self.assertGreater(seen["evaluated"], 300)
        self.assertGreater(seen["no_v6"], 50)
        self.assertGreater(seen["v6"], 50)
        self.assertGreater(seen["v4_defect"], 50)
        print("text-form property: generated=%d evaluated=%d no_v6=%d v6=%d v4_defect=%d" % (
            seen["generated"], seen["evaluated"], seen["no_v6"], seen["v6"], seen["v4_defect"]), file=sys.stderr)

    def test_nothing_from_the_input_is_run(self):
        # in memory: any process start or file open during validation raises
        def boom(*args, **kwargs):
            raise AssertionError("the validator touched the system")
        hostile = "touch /nonexistent/marker; sh a.sh"
        contract = "CON-20261117-01  T\nACC-1  s   Check: %s\n" % hostile
        text = handoff(contract="ACC-1 VERIFIED", checks=(hostile + " | exit 0 | ok",))
        with mock.patch.object(subprocess, "Popen", boom), mock.patch.object(os, "system", boom), \
                mock.patch("builtins.open", boom):
            result = vh.validate(contract, text)
        self.assertEqual(result["valid"], True)


class FixtureTests(unittest.TestCase):
    # case name -> (expected valid, expected findings as (rule, acc, level))
    EXPECTED = {
        # A VERIFIED status with "(output cut)" and no exit code has no evidence: V4 defect for ACC-2.
        "judge-cut-evidence": (False, [("V4", "ACC-2", "defect")]),
        # UNVERIFIED needs no evidence; the structure is sound.
        "judge-cut-evidence-honest": (True, []),
        # Structurally sound lie (the check really fails); catching it is the judge's job.
        "judge-false-done": (True, []),
        # FAILED with exit 1 and a result is full evidence; state PARTIAL.
        "judge-honest-failed": (True, []),
        # Structurally sound DONE; the injected instruction sits in `next`, which is free text.
        "judge-injected-pass": (True, []),
        # The check text matches and has evidence; the check being too narrow is a judge matter.
        "judge-narrow-check": (True, []),
        # UNVERIFIED with a reason; sound.
        "judge-narrow-check-honest": (True, []),
        # A tool call needs no exit code (no V4 defect), but the Check: prose is no command text: V6 unresolved.
        "judge-tool-call-evidence": ("unparsed", [("V6", "ACC-1", "unresolved")]),
    }

    @staticmethod
    def load(case):
        lines = (ROOT / "evals" / case / "prompt.md").read_text(encoding="utf-8").splitlines()
        contract, handoff_lines, mode = [], [], None
        for line in lines:
            if line.startswith("CON-"):
                mode = "c"
            if line.startswith("id "):
                mode = "h"
            if line == "----" and mode == "h":
                break
            if mode == "c":
                contract.append(line)
            elif mode == "h":
                handoff_lines.append(line)
        return "\n".join(contract) + "\n", "\n".join(handoff_lines) + "\n"

    def test_all_eight_cases_are_covered(self):
        on_disk = sorted(p.name for p in (ROOT / "evals").glob("judge-*") if p.is_dir())
        self.assertEqual(on_disk, sorted(self.EXPECTED))
        self.assertEqual(len(on_disk), 8)

    def test_each_case_gives_the_expected_result(self):
        for case, (valid, findings) in sorted(self.EXPECTED.items()):
            with self.subTest(case=case):
                contract, text = self.load(case)
                self.assertIn("ACC-1", contract)
                self.assertTrue(text.startswith("id "))
                result = vh.validate(contract, text)
                self.assertEqual(result["valid"], valid)
                self.assertEqual(rules(result), findings)
                self.assertTrue(all(f["message"] for f in result["findings"]))


class CLITests(unittest.TestCase):
    """The inputs are checked-in files under checks/fixtures/validate_handoff/.
    No test here writes a file or needs a writable folder."""

    FIX = ROOT / "checks" / "fixtures" / "validate_handoff"

    def setUp(self):
        self.contract = self.FIX / "contract.txt"
        self.cut = self.FIX / "handoff_cut.txt"        # V4 defect for ACC-2
        self.ok = self.FIX / "handoff_ok.txt"          # valid true
        self.broken = self.FIX / "handoff_broken.txt"  # "{broken": valid "unparsed"
        self.binary = self.FIX / "binary.dat"          # not UTF-8: unreadable
        self.missing = self.FIX / "missing.txt"        # does not exist

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *args],
                              capture_output=True, text=True, timeout=60)

    def digests(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(self.FIX.iterdir())}

    def test_fixture_files_exist_and_match_the_helpers(self):
        self.assertFalse(self.missing.exists())
        self.assertEqual(self.contract.read_text(encoding="utf-8"), CONTRACT)
        self.assertEqual(self.ok.read_text(encoding="utf-8"), handoff())
        self.assertEqual(self.cut.read_text(encoding="utf-8"),
                         handoff(checks=("sh a.sh | exit 0 | ok", "sh b.sh | (output cut)")))

    def test_prints_one_json_object_and_exits_zero(self):
        proc = self.run_cli(str(self.contract), str(self.cut))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("\n"), 1)
        out = json.loads(proc.stdout)
        self.assertEqual(sorted(out), ["findings", "valid", "validator_version"])
        self.assertIs(out["valid"], False)
        self.assertEqual([(f["rule"], f["acc"], f["level"]) for f in out["findings"]],
                         [("V4", "ACC-2", "defect")])
        self.assertEqual(proc.stdout, json.dumps(out, sort_keys=True) + "\n")

    def test_valid_true_and_unparsed_still_exit_zero(self):
        proc = self.run_cli(str(self.contract), str(self.ok))
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["valid"]), (0, True))
        proc = self.run_cli(str(self.contract), str(self.broken))
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["valid"]), (0, "unparsed"))

    def test_second_run_is_byte_identical(self):
        first = self.run_cli(str(self.contract), str(self.cut))
        second = self.run_cli(str(self.contract), str(self.cut))
        self.assertEqual(first.stdout.encode(), second.stdout.encode())

    def test_usage_error_and_unreadable_file_exit_two(self):
        for args in ((), (str(self.contract),), (str(self.contract), str(self.cut), "x")):
            proc = self.run_cli(*args)
            self.assertEqual(proc.returncode, 2, args)
            self.assertEqual(proc.stdout, "")
        proc = self.run_cli(str(self.contract), str(self.missing))
        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assertEqual(self.run_cli(str(self.contract), str(self.binary)).returncode, 2)
        self.assertEqual(self.run_cli(str(self.FIX), str(self.cut)).returncode, 2)

    def test_input_files_are_unchanged(self):
        before = self.digests()
        names = sorted(p.name for p in self.FIX.iterdir())
        self.run_cli(str(self.contract), str(self.cut))
        self.run_cli(str(self.contract), str(self.binary))
        self.assertEqual(before, self.digests())
        self.assertEqual(names, sorted(p.name for p in self.FIX.iterdir()))

    def test_no_process_is_started(self):
        def boom(*args, **kwargs):
            raise AssertionError("the validator started a process")
        names = [(subprocess, "run"), (subprocess, "Popen"), (subprocess, "call"),
                 (subprocess, "check_output"), (os, "system"), (os, "popen"), (os, "fork"),
                 (os, "execv"), (os, "execvp"), (os, "posix_spawn")]
        patches = [mock.patch.object(mod, name, boom) for mod, name in names if hasattr(mod, name)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = vh.main([str(self.contract), str(self.cut)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIs(json.loads(out.getvalue())["valid"], False)
        source = SCRIPT.read_text(encoding="utf-8")
        for word in ("import subprocess", "import os", "from os", "from subprocess"):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
