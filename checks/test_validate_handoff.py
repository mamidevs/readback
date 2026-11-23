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
import re
from pathlib import Path
import subprocess
import sys
import types
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


def F(rule, acc, level, message):
    return {"rule": rule, "acc": acc, "level": level, "message": message}


BASE_SHA = "e49f439fc310df5cc8f0eb8f56e3d40b7b586aee"
BASE_BLOB_SHA256 = "90b8b3ea90d4c00caebb570d71f0ebcf494388623a26d02b18de901a970f6911"
_V1 = []


def v1():
    """The validator of version 1.1.0, a frozen copy of scripts/validate_handoff.py at BASE_SHA.

    The file is checked against its sha256 before it is loaded. No Git command is run."""
    if not _V1:
        path = ROOT / "checks" / "fixtures" / "validate_handoff" / "v1.1.0" / "validate_handoff.py"
        if hashlib.sha256(path.read_bytes()).hexdigest() != BASE_BLOB_SHA256:
            raise AssertionError("the frozen file is not the validator of version 1.1.0")
        spec = importlib.util.spec_from_file_location("validate_handoff_v1_frozen", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _V1.append(module)
    return _V1[0]


def colon_handoff(colon=(), state="DONE", contract="ACC-1 VERIFIED | ACC-2 VERIFIED",
                  checks=("sh a.sh | exit 0 | ok", "sh b.sh | exit 0 | ok"), drop=(), override=None, extra=""):
    """The handoff() rows, with a colon after each key named in `colon` (all keys for "all").
    `override` maps a key to its whole line text."""
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
        head = key + ":" if colon == "all" or key in colon else key
        if override and key in override:
            lines.append(override[key])
        elif key == "checks":
            lines.append("%-9s %s" % (head, checks[0]))
            lines.extend("          " + c for c in checks[1:])
        else:
            lines.append("%-9s %s" % (head, value))
    return "\n".join(lines) + "\n" + extra


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

    def test_version_and_docstring_describe_version_2(self):
        self.assertEqual(vh.VALIDATOR_VERSION, "2.2.0")
        self.assertEqual(vh.validate(CONTRACT, handoff())["validator_version"], "2.2.0")
        doc = " ".join(vh.__doc__.split())
        # the five section starts, in the words of D1
        self.assertIn("`Limits`, `Definitions`, `Evidence requirements`, `Coverage list` or `Command lines`", doc)
        for name in ("Limits", "Definitions", "Evidence requirements", "Coverage list", "Command lines"):
            self.assertIn("`%s`" % name, doc)
        self.assertIn("Sections of a contract (version 2). After the line `Out of scope:`", doc)
        self.assertIn("`extra[\"sections\"]` as a list of `{\"name\", \"lines\"}`", doc)
        self.assertIn("`a contract line stands inside a section`", doc)
        # the colon form, in the words of D4
        self.assertIn("Colon keys of a handoff (version 2). A key of PROTOCOL.md 3.3 at column 0 may be followed "
                      "directly by one colon (`summary: text`)", doc)
        self.assertIn("so `summary: task: x` and `summary task: x` both hold `task: x`", doc)
        self.assertIn("a key that stands twice in either form is repeated", doc)
        self.assertIn("The status bullets `- ACC-<n> <STATUS>` below `contract` are read for V3 and V5", doc)
        self.assertIn("A kept line never changes how `state` is read, and below `checks` it is read as evidence by "
                      "its own text but is never taken for the command of a Check.", doc)
        self.assertIn("In a handoff that holds a colon key only the keys of 3.3 are keys", doc)
        self.assertIn("a handoff that holds a colon key has no extra key", doc)

    def test_load_json_reads_objects_only(self):
        pairs, err = vh._load_json('{"a": 1, "a": 2}')
        self.assertEqual((list(pairs), err), ([("a", 1), ("a", 2)], None))
        self.assertIsInstance(pairs, vh._Pairs)
        for text in ("[1]", '"x"', "1", "null"):
            self.assertEqual(vh._load_json(text), (None, "JSON input is not an object"), text)
        obj, err = vh._load_json("{bad")
        self.assertIsNone(obj)
        self.assertTrue(err.startswith("invalid JSON: "), err)

    def test_value_text_forms(self):
        self.assertEqual(vh._value_text("a"), "a")
        self.assertEqual(vh._value_text(["a", ["b", "c"]]), "a\nb\nc")
        self.assertEqual(vh._value_text([]), "")
        self.assertEqual(vh._value_text(5), "5")
        self.assertEqual(vh._value_text(None), "null")
        self.assertEqual(vh._value_text(True), "true")
        self.assertEqual(vh._value_text({"b": 1, "a": 2}), '{"a": 2, "b": 1}')
        self.assertEqual(vh._value_text(vh._Pairs([("k", "v"), ("j", ["x", "y"])])), "k v | j x\ny")

    def test_statement_trims_only_ascii_space_and_tab(self):
        self.assertEqual(vh._statement("ACC-1", "s", " \t x y \t "),
                         {"id": "ACC-1", "statement": "s", "check": "x y", "match": "x y"})
        self.assertEqual(vh._statement("ACC-1", "s", None),
                         {"id": "ACC-1", "statement": "s", "check": None, "match": None})
        self.assertEqual(vh._statement("ACC-1", "s", " \t "),
                         {"id": "ACC-1", "statement": "s", "check": "", "match": None})
        self.assertEqual(vh._statement("ACC-1", "s", "a\nb"),
                         {"id": "ACC-1", "statement": "s", "check": "a\nb", "match": None})
        self.assertEqual(vh._statement("ACC-1", "s", "\u00a0x")["match"], "\u00a0x")

    def test_semicolon_separates_status_entries_in_the_handoff(self):
        canonical = vh.validate(CONTRACT, handoff())
        self.assertIs(canonical["valid"], True)
        for sep in ("; ", ";"):
            with self.subTest(sep=sep):
                self.assertEqual(vh.validate(CONTRACT, handoff(contract=sep.join(
                    ["ACC-1 VERIFIED", "ACC-2 VERIFIED"]))), canonical)
        for tail in ("ACC-2 FAILED (test failed)", "ACC-2 MISSPELLED", "ACC-1 VERIFIED"):
            expected = vh.validate(CONTRACT, handoff(contract="ACC-1 VERIFIED | " + tail))
            self.assertIs(expected["valid"], False)
            self.assertEqual(vh.validate(CONTRACT, handoff(contract="ACC-1 VERIFIED; " + tail)), expected)

    def test_semicolon_inside_a_status_reason_is_literal(self):
        reason = "checks (one; two); ACC-3 FAILED"
        self.assertEqual(vh.parse_statuses("ACC-1 UNVERIFIED (" + reason + "); - ACC-2 VERIFIED"),
                         ([("ACC-1", "UNVERIFIED", reason), ("ACC-2", "VERIFIED", None)], []))

    def test_parse_statuses_forms(self):
        self.assertEqual(vh.parse_statuses("ACC-1 VERIFIED\n\nACC-2 FAILED (x) | - ACC-3 UNVERIFIED\n- ACC-4 FAILED"),
                         ([("ACC-1", "VERIFIED", None), ("ACC-2", "FAILED", "x"),
                           ("ACC-3", "UNVERIFIED", None), ("ACC-4", "FAILED", None)], []))
        for none in ("none", " NONE ", "\u2014", "-", " - "):
            self.assertEqual(vh.parse_statuses(none), ([], []), none)
        self.assertEqual(vh.parse_statuses("ACC-1 VERIFIED because\n\nx"),
                         ([], ["ACC-1 VERIFIED because", "x"]))

    def test_json_contract_reading(self):
        text = json.dumps({"id": "CON-20261117-01", "task": "t", "out_of_scope": "o",
                           "ACC-1": "s   Check: sh a.sh", "note": ["a", "b"], "ACC-2": {"statement": "s2", "check": "sh b.sh"}})
        parsed = vh.parse_contract(text)
        self.assertEqual((parsed["id"], parsed["task"], parsed["out_of_scope"], parsed["extra"], parsed["problems"]),
                         ("CON-20261117-01", "t", "o", {"note": "a\nb"}, []))
        self.assertEqual([(s["id"], s["statement"], s["check"]) for s in parsed["statements"]],
                         [("ACC-1", "s", "sh a.sh"), ("ACC-2", "s2", "sh b.sh")])
        self.assertEqual(vh.validate(text, handoff()), {"valid": True, "findings": [], "validator_version": "2.2.0"})
        spaced = vh.parse_contract(json.dumps({"id": "CON-20261117-01", "Out of scope": "o2"}))
        self.assertEqual((spaced["out_of_scope"], spaced["extra"]), ("o2", {}))
        notlist = vh.parse_contract(json.dumps({"id": "CON-20261117-01", "acc": "text", "statements": 5}))
        self.assertEqual((notlist["extra"], notlist["statements"], notlist["problems"]),
                         ({"acc": "text", "statements": "5"}, [], []))

    def test_json_contract_lists_of_statements(self):
        for key in ("acc", "statements"):
            text = json.dumps({"id": "CON-20261117-01", key: [
                "x", {"id": "ACC-1", "statement": "s", "check": "sh a.sh"},
                {"id": "ACC-2", "text": "t2"}]})
            parsed = vh.parse_contract(text)
            self.assertEqual(parsed["problems"], ["an item of `%s` is not an object" % key])
            self.assertEqual([(s["id"], s["statement"], s["check"]) for s in parsed["statements"]],
                             [("ACC-1", "s", "sh a.sh"), ("ACC-2", "t2", None)])
            self.assertEqual(vh.validate(text, handoff(state="PARTIAL", contract="ACC-1 UNVERIFIED | ACC-2 UNVERIFIED"))["findings"],
                             [F("V1", None, "unresolved", "an item of `%s` is not an object" % key),
                              F("V1", "ACC-2", "defect", "ACC-2 has no Check:")])
        absent = vh.parse_contract(json.dumps({"id": "CON-20261117-01", "ACC-1": {"statement": "s"}}))
        self.assertIsNone(absent["statements"][0]["check"])

    def test_json_contract_with_a_bad_acc_id_is_a_defect_only_for_that_id(self):
        text = json.dumps({"id": "CON-20261117-01", "acc": [
            {"id": "X1", "statement": "s", "check": "c"},
            {"id": "ACC-1", "statement": "s", "check": "sh a.sh"},
            {"id": "ACC-2", "statement": "s", "check": "sh b.sh"}]})
        result = vh.validate(text, handoff())
        self.assertEqual(result["findings"], [F("V1", None, "defect", "ACC id 'X1' is not ACC-<n>")])
        self.assertIs(result["valid"], False)

    def test_unreadable_json_contract_is_unresolved(self):
        for text, message in (("{}", "the contract object is empty"),):
            result = vh.validate(text, handoff())
            self.assertEqual(result["findings"], [F("V1", None, "unresolved", message)])
            self.assertEqual(result["valid"], "unparsed")
            self.assertEqual(vh.parse_contract(text)["ok"], False)
        result = vh.validate("{bad", handoff())
        self.assertEqual([(f["rule"], f["acc"], f["level"]) for f in result["findings"]], [("V1", None, "unresolved")])
        self.assertTrue(result["findings"][0]["message"].startswith("invalid JSON: "))
        self.assertEqual(result["valid"], "unparsed")
        self.assertFalse(vh.parse_contract("{bad")["ok"])

    def test_text_contract_reading_details(self):
        two = "CON-20261117-01  T one\n   and more\nCON-20261117-02  U\nACC-1  s   Check: sh a.sh\nACC-2  s   Check: sh b.sh\n"
        parsed = vh.parse_contract(two)
        self.assertEqual((parsed["id"], parsed["ids"], parsed["task"]),
                         ("CON-20261117-01", ["CON-20261117-01", "CON-20261117-02"], "T one and more"))
        self.assertEqual(vh.validate(two, handoff())["findings"],
                         [F("V1", None, "defect", "the contract has 2 CON ids")])
        bare = vh.parse_contract("CON-20261117-01\n   Task here\nACC-1 s  Check: x\n")
        self.assertEqual(bare["task"], "Task here")
        odd = CONTRACT.replace("Out of scope", "ACC-1a  s   Check: x\nOut of scope")
        result = vh.validate(odd, handoff())
        self.assertEqual(result["findings"], [F("V1", None, "unresolved", "ACC id cannot be read: 'ACC-1a'")])
        self.assertEqual(result["valid"], "unparsed")
        self.assertEqual([s["id"] for s in vh.parse_contract(odd)["statements"]], ["ACC-1", "ACC-2"])

    def test_text_continuation_lines_and_empty_values(self):
        parsed = vh.parse_handoff("id  a\n\tb\n c\nnext\n   d\n")
        self.assertEqual(parsed["values"], {"id": "a\nb\nc", "next": "d"})
        self.assertEqual(parsed["problems"], [])
        result = vh.validate(CONTRACT, handoff(extra="!\n"))
        self.assertEqual(result["findings"], [F("V2", None, "unresolved", "line at column 0 is not a key: '!'")])

    def test_messages_cut_long_text_at_the_stated_length(self):
        long = "!" + "y" * 69
        result = vh.validate(CONTRACT, handoff(extra=long + "\n"))
        self.assertEqual(result["findings"], [F("V2", None, "unresolved", "line at column 0 is not a key: %r" % long[:60])])
        result = vh.validate(CONTRACT, "   " + long + "\n" + handoff())
        self.assertEqual(result["findings"], [F("V2", None, "unresolved",
                                                "continuation line before any key: %r" % long[:60])])
        result = vh.validate(CONTRACT, handoff(state="Z" * 50))
        self.assertEqual(result["findings"], [F("V2", None, "defect",
                                                "state %r is not DONE, PARTIAL, BLOCKED or UNABLE" % ("Z" * 40))])
        result = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="Q" * 70))
        self.assertEqual(result["findings"], [
            F("V3", None, "unresolved", "the contract status %r cannot be read as `ACC-<n> <STATUS>`" % ("Q" * 60)),
            F("V3", "ACC-1", "unresolved", "ACC-1 has no status"),
            F("V3", "ACC-2", "unresolved", "ACC-2 has no status")])
        check = "sh " + "x" * 67
        contract = "CON-20261117-01  T\nACC-1  s   Check: %s\n" % check
        result = vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=(check + " | exit 0",)))
        self.assertEqual(result["findings"], [F("V4", "ACC-1", "defect",
                                                "ACC-1 has a status of VERIFIED but the command has no result: %r" % check[:60])])

    def test_json_handoff_reads_its_own_values_as_they_are(self):
        keys = dict(self.plain_values(), state="DONE")
        parsed = vh.parse_handoff(json.dumps(keys))
        self.assertEqual(parsed["own_values"], parsed["values"])
        self.assertEqual(parsed["own_values"]["state"], "DONE")

    @staticmethod
    def plain_values():
        return vh.parse_handoff(handoff())["values"]

    def test_json_checks_list_drops_blank_items(self):
        keys = vh.parse_handoff(handoff())["values"]
        obj = dict(keys, checks=["", "sh a.sh | exit 0 | ok", " \t ", "sh b.sh | exit 0 | ok\nx"])
        parsed = vh.parse_handoff(json.dumps(obj))
        self.assertEqual(parsed["checks_lines"], [{"text": "sh a.sh | exit 0 | ok", "multiline": False, "kept": False},
                                                  {"text": "sh b.sh | exit 0 | ok\nx", "multiline": True, "kept": False}])


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

    def test_v2_a_missing_key_is_the_only_finding_whichever_key(self):
        for key in ("state", "contract", "checks"):
            result = vh.validate(CONTRACT, handoff(drop=(key,)))
            self.assertEqual(result["findings"], [F("V2", None, "defect", "the handoff has no `%s` key" % key)], key)
            self.assertIs(result["valid"], False)

    def test_v2_a_repeated_key_out_of_order_is_not_also_an_order_defect(self):
        result = vh.validate(CONTRACT, handoff(extra="from      again\n"))
        self.assertEqual(result["findings"], [F("V2", None, "defect", "the key `from` appears 2 times")])
        lines = handoff().splitlines()
        lines[0], lines[1] = lines[1], lines[0]
        self.assertEqual(vh.validate(CONTRACT, "\n".join(lines) + "\n")["findings"], [
            F("V2", None, "defect", "the handoff keys are not in the order of PROTOCOL.md 3.3")])

    def test_v3_none_forms_give_a_missing_status_for_each_acc(self):
        for word in ("none", "NONE", " - ", "\u2014"):
            result = vh.validate(CONTRACT, handoff(state="PARTIAL", contract=word))
            self.assertEqual(result["findings"], [F("V3", "ACC-1", "defect", "ACC-1 has no status"),
                                                  F("V3", "ACC-2", "defect", "ACC-2 has no status")], word)

    def test_v5_done_with_a_missing_status_gives_v3_and_v5(self):
        result = vh.validate(CONTRACT, handoff(contract="ACC-1 VERIFIED"))
        self.assertEqual(result["findings"], [F("V3", "ACC-2", "defect", "ACC-2 has no status"),
                                              F("V5", "ACC-2", "defect", "state DONE but ACC-2 has no status")])
        result = vh.validate(CONTRACT, handoff(contract="ACC-1 VERIFIED | ACC-2 UNVERIFIED (x)"))
        self.assertEqual(result["findings"], [F("V5", "ACC-2", "defect", "state DONE but ACC-2 is not VERIFIED")])

    def test_messages_of_v3(self):
        result = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 PASSED | ACC-2 VERIFIED | ACC-2 FAILED | ACC-9 FAILED",
                                              checks=("sh a.sh | exit 0 | ok", "sh b.sh | exit 0 | ok")))
        self.assertEqual(result["findings"], [
            F("V3", "ACC-1", "defect", "status 'PASSED' of ACC-1 is not VERIFIED, FAILED or UNVERIFIED"),
            F("V3", "ACC-2", "defect", "ACC-2 has 2 statuses"),
            F("V3", "ACC-9", "defect", "ACC-9 is not an ACC of the contract")])


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

    def line(self, text, multiline=False):
        return vh.parse_evidence_line(text, multiline)

    @staticmethod
    def info(command, matchable, complete, ambiguous, doubtful, why):
        return {"command": command, "matchable": matchable, "complete": complete,
                "ambiguous": ambiguous, "doubtful": doubtful, "why": why}

    def test_parse_evidence_line_gives_every_field(self):
        i = self.info
        cases = [
            ("sh a.sh | exit 0 | ok", i("sh a.sh", True, True, False, False, None)),
            ("sh b.sh | exit 0 | ", i("sh b.sh", True, False, False, False, "the command has no result")),
            ("sh b.sh | exit 0", i("sh b.sh", True, False, False, False, "the command has no result")),
            ("a | exit 0 | (output cut)", i("a", True, False, True, False, None)),
            ("sh b.sh | (output cut)", i("sh b.sh", True, False, False, False, "the command has no exit code")),
            ("sh b.sh", i("sh b.sh", True, False, False, False, "the command has no exit code")),
            ("printf tool | exit 0 | tool call", i("printf tool", True, True, False, False, None)),
            ("Grep tool, x | exit 0 | ok", i(None, False, False, False, True, "the line has an exit code and a tool call")),
            ("Grep tool, pattern 'x'", i("Grep tool, pattern 'x'", True, False, False, False, "the tool call has no result")),
            ("Grep tool, x | other", i(None, False, False, False, True, "the tool call line cannot be read")),
            ("Grep tool, x | no exit code (tool call) | one",
             i("Grep tool, x", True, True, False, False, None)),
            ("X | no exit code (tool call) | res", i("X", True, True, False, False, None)),
            ("X | no exit code (tool call)", i("X", True, False, False, False, "the tool call has no result")),
            ("X | no exit code (tool call) | (output cut)", i("X", True, False, True, False, None)),
            ("a | b | c", i(None, False, False, False, False, "the command of the line cannot be told")),
            ("a | exit 1 | exit 2 | ok", i(None, False, False, False, True, "the line has more than one exit-code segment")),
            ("Grep tool, x | exit code 3 mentioned",
             i(None, False, False, False, True, "the tool call line mentions an exit code that is not a segment")),
            ("a b exit 5", i(None, False, False, False, True, "the line mentions an exit code that is not a segment")),
        ]
        for text, expected in cases:
            self.assertEqual(self.line(text), expected, text)
        self.assertEqual(self.line("a\nb", True),
                         i(None, False, False, False, True, "the evidence line holds a newline"))

    def test_a_result_that_names_a_tool_call_is_complete_evidence(self):
        contract = "CON-20261117-01  T\nACC-1  s   Check: printf tool\n"
        result = vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=("printf tool | exit 0 | tool call",)))
        self.assertEqual(result, {"valid": True, "findings": [], "validator_version": "2.2.0"})
        contract = "CON-20261117-01  T\nACC-1  s   Check: Grep tool, x\n"
        for line in ("Grep tool, x | no exit code (tool call) | tool call", "Grep tool, x | no exit code (tool call) | one"):
            result = vh.validate(contract, handoff(contract="ACC-1 VERIFIED", checks=(line,)))
            self.assertEqual(result, {"valid": True, "findings": [], "validator_version": "2.2.0"}, line)

    def test_v4_messages(self):
        # a FAILED status with a cut marker: unresolved, and the status word is shown
        result = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 VERIFIED | ACC-2 FAILED (x)",
                                              checks=("sh a.sh | exit 0 | ok", "sh b.sh | exit 1 | (output cut)")))
        self.assertEqual(result["findings"], [F("V4", "ACC-2", "unresolved",
                         "ACC-2 has a status of FAILED but the result of its line looks like a cut marker")])
        # three statuses of one ACC: the words other than UNVERIFIED are shown, sorted
        result = vh.validate(CONTRACT, handoff(state="PARTIAL",
                                              contract="ACC-1 VERIFIED | ACC-2 VERIFIED | ACC-2 FAILED | ACC-2 UNVERIFIED",
                                              checks=("sh a.sh | exit 0 | ok", "sh b.sh | exit 1 | (output cut)")))
        self.assertEqual(result["findings"], [
            F("V3", "ACC-2", "defect", "ACC-2 has 3 statuses"),
            F("V4", "ACC-2", "unresolved",
              "ACC-2 has a status of FAILED/VERIFIED but the result of its line looks like a cut marker")])
        # a missing result
        result = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 VERIFIED | ACC-2 FAILED (x)",
                                              checks=("sh a.sh | exit 0 | ok", "sh b.sh | exit 1")))
        self.assertEqual(result["findings"], [F("V4", "ACC-2", "defect",
                         "ACC-2 has a status of FAILED but the command has no result: 'sh b.sh'")])
        # no evidence at all
        result = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 UNVERIFIED | ACC-2 FAILED (x)", checks=("none",)))
        self.assertEqual(result["findings"], [F("V4", "ACC-2", "defect", "ACC-2 has no evidence line in checks")])

    def test_v4_failed_status_with_unrelated_evidence_is_unresolved_not_v6(self):
        result = vh.validate(CONTRACT, handoff(state="PARTIAL", contract="ACC-1 VERIFIED | ACC-2 FAILED (x)",
                                              checks=("sh a.sh | exit 0 | ok", "other | exit 1 | bad")))
        self.assertEqual(result["findings"], [F("V4", "ACC-2", "unresolved",
                         "no checks line can be tied to the Check: text of ACC-2")])
        self.assertEqual(result["valid"], "unparsed")
        # a VERIFIED status gives V6 only
        result = vh.validate(CONTRACT, handoff(checks=("sh a.sh | exit 0 | ok", "other | exit 1 | bad")))
        self.assertEqual(result["findings"], [F("V6", "ACC-2", "unresolved",
                         "no checks line has the Check: text of ACC-2 as its command")])

    def test_an_acc_without_a_check_needs_no_evidence_line(self):
        contract = CONTRACT.replace("ACC-2  The tool shall pass.   Check: sh b.sh", "ACC-2  The tool shall pass.")
        result = vh.validate(contract, handoff())
        self.assertEqual(result["findings"], [F("V1", "ACC-2", "defect", "ACC-2 has no Check:")])
        self.assertIs(result["valid"], False)

    def test_a_repeated_acc_uses_its_first_check(self):
        contract = CONTRACT + "ACC-2  Again.   Check: sh other.sh\n"
        contract = contract.replace("Out of scope: everything else\nACC-2", "ACC-2")
        result = vh.validate(contract, handoff())
        self.assertEqual(result["findings"], [F("V1", "ACC-2", "defect", "ACC-2 repeats in the contract")])
        contract = contract.replace("sh b.sh", "sh c.sh")
        self.assertEqual(vh.validate(contract, handoff(contract="ACC-1 VERIFIED | ACC-2 UNVERIFIED (x)", state="PARTIAL"))["findings"],
                         [F("V1", "ACC-2", "defect", "ACC-2 repeats in the contract")])

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


REAL = ROOT / "checks" / "fixtures" / "validate_handoff" / "real"
FROZEN = {
    "CON-20261118-01.txt": "11b091db7e1006517cc497188e1d0eadaf8af70e1769c55a3f67b2dc4867b6b9",
    "CON-20261118-08.txt": "bed2ec42cb2447fc4ba1311a299952c9d8d1bbc19c6e564fa17608c3e47ef0e4",
    "CON-20261118-11.txt": "a7c8ade0ecbebe9145ed015e2ce8966e2b4a554b76e0f760680838136f7c1132",
    "HND-20261117-47.txt": "c7f1ad0d3d02b5374d6e60cb5101daec7f1fc748c4d3090b23463d7881563c58",
}
REAL_CONTRACTS = ("CON-20261118-01", "CON-20261118-08", "CON-20261118-11")


def ok_result(valid=True, findings=()):
    return {"valid": valid, "findings": list(findings), "validator_version": "2.2.0"}


class SectionTests(unittest.TestCase):
    """D1 to D3 and D7: the sections that a contract holds after `Out of scope:`."""

    UNRESOLVED = "a line of the section %s begins with ACC- or CON-: %r"
    DEFECT = F("V1", None, "defect", "a contract line stands inside a section")

    @staticmethod
    def contract(*tail, scope="Out of scope: everything else"):
        return CONTRACT.replace("Out of scope: everything else", scope) + "".join(line + "\n" for line in tail)

    def check(self, text, valid, findings, sections):
        self.assertEqual(vh.validate(text, handoff()), ok_result(valid, findings))
        self.assertEqual(vh.parse_contract(text)["extra"].get("sections"), sections)

    def test_s1_three_keys_only_is_version_1(self):
        self.check(CONTRACT, True, [], None)
        self.assertEqual(vh.parse_contract(CONTRACT)["extra"], {})
        self.assertEqual(v1().validate(CONTRACT, handoff())["findings"], [])

    def test_s2_limits_after_out_of_scope(self):
        line = "Limits, stated on purpose: the validator reads structure; it does not judge the text."
        text = self.contract(line)
        self.check(text, True, [], [{"name": "Limits", "lines": [line]}])
        parsed = vh.parse_contract(text)
        self.assertEqual(parsed["out_of_scope"], "everything else")
        self.assertEqual([s["id"] for s in parsed["statements"]], ["ACC-1", "ACC-2"])

    def test_s3_definitions_with_lines_at_column_0(self):
        lines = ["Definitions", "D1. The first definition.", "D2. The second, which names ACC-1 in its text."]
        self.check(self.contract(*lines), True, [], [{"name": "Definitions", "lines": lines}])

    def test_s4_evidence_requirements_with_list_items(self):
        lines = ["Evidence requirements for the handoff", "- E1. Each check is run.", "- E2. A table of rows."]
        self.check(self.contract(*lines), True, [], [{"name": "Evidence requirements", "lines": lines}])

    def test_s5_coverage_list_with_a_table(self):
        lines = ["Coverage list", "| Piece | Contract |", "|---|---|", "| W1 | CON-20261117-21 |"]
        self.check(self.contract(*lines), True, [], [{"name": "Coverage list", "lines": lines}])

    def test_s6_command_lines_with_a_long_line(self):
        long = " \u00b7 ".join("C%d `git commit -m M%d`" % (i, i) for i in range(1, 60))
        self.assertGreater(len(long), 1000)
        lines = ["Command lines for ACC-1 (`M` stands for a text that the test defines)", long]
        self.check(self.contract(*lines), True, [], [{"name": "Command lines", "lines": lines}])

    def test_s7_all_five_sections_in_one_contract(self):
        tail = ["Limits, stated on purpose: one.", "", "Coverage list", "| Piece | Contract |", "|---|---|", "",
                "Definitions", "D1. First.", "D2. Second.", "", "Command lines for ACC-1 (`M`)", "C1 `git commit -m M`", "",
                "Evidence requirements for the handoff", "- E1. Each check is run.", "- E2. A table."]
        sections = [
            {"name": "Limits", "lines": ["Limits, stated on purpose: one.", ""]},
            {"name": "Coverage list", "lines": ["Coverage list", "| Piece | Contract |", "|---|---|", ""]},
            {"name": "Definitions", "lines": ["Definitions", "D1. First.", "D2. Second.", ""]},
            {"name": "Command lines", "lines": ["Command lines for ACC-1 (`M`)", "C1 `git commit -m M`", ""]},
            {"name": "Evidence requirements", "lines": ["Evidence requirements for the handoff",
                                                        "- E1. Each check is run.", "- E2. A table."]},
        ]
        self.check(self.contract(*tail), True, [], sections)
        self.assertEqual("\n".join(l for s in sections for l in s["lines"]) + "\n",
                         "\n".join(tail) + "\n")

    def test_s8_a_section_keeps_blank_and_indented_lines(self):
        lines = ["Definitions", "D1. First.", "", "    an indented line", "\tand one with a tab", "D2. Second.", "   "]
        self.check(self.contract(*lines), True, [], [{"name": "Definitions", "lines": lines}])

    def test_s9_an_acc_line_inside_a_section_is_a_defect(self):
        text = self.contract("Definitions", "D1. First.", "ACC-6  The tool shall x.   Check: sh c.sh", "D2. Second.")
        self.check(text, False, [self.DEFECT],
                   [{"name": "Definitions", "lines": ["Definitions", "D1. First.", "ACC-6  The tool shall x.   Check: sh c.sh",
                                                      "D2. Second."]}])
        self.assertEqual([s["id"] for s in vh.parse_contract(text)["statements"]], ["ACC-1", "ACC-2"])

    def test_s10_a_con_line_inside_a_section(self):
        text = self.contract("Limits, stated on purpose: x", "CON-20261118-99  Another task")
        self.check(text, False, [self.DEFECT],
                   [{"name": "Limits", "lines": ["Limits, stated on purpose: x", "CON-20261118-99  Another task"]}])
        self.assertEqual(vh.parse_contract(text)["ids"], ["CON-20261117-01"])
        # the literal line CON-... is no id with a digit: unresolved
        text = self.contract("Limits, stated on purpose: x", "CON-...")
        self.check(text, "unparsed", [F("V1", None, "unresolved", self.UNRESOLVED % ("Limits", "CON-..."))],
                   [{"name": "Limits", "lines": ["Limits, stated on purpose: x", "CON-..."]}])

    def test_s11_a_line_between_out_of_scope_and_the_first_section(self):
        text = self.contract("Notes  text", "Limits x")
        self.check(text, True, [], [{"name": "Limits", "lines": ["Limits x"]}])
        self.assertEqual(vh.parse_contract(text)["extra"]["Notes"], "text")
        text = self.contract("!! Notes", "Limits x")
        self.check(text, "unparsed", [F("V1", None, "unresolved", "line at column 0 is not a key: '!! Notes'")],
                   [{"name": "Limits", "lines": ["Limits x"]}])
        self.assertEqual(vh.parse_contract(text)["extra"], {"sections": [{"name": "Limits", "lines": ["Limits x"]}]})

    def test_s12_a_section_word_in_the_middle_of_a_line_is_no_section(self):
        for scope in ("Out of scope: the Limits of the old text, Definitions and the Coverage list",
                      "Out of scope: x\n  Limits go on here\n\tDefinitions too"):
            text = self.contract(scope=scope)
            self.check(text, True, [], None)
            self.assertEqual(v1().validate(text, handoff())["findings"], [])
            self.assertEqual(vh.parse_contract(text)["out_of_scope"], v1().parse_contract(text)["out_of_scope"])
            self.assertEqual(vh.parse_contract(text)["extra"], v1().parse_contract(text)["extra"])

    def test_s13_out_of_scope_without_a_value_then_limits(self):
        lines = ["Limits: stated on purpose", "  ACC-9  an indented line", "ACCEPTED yes"]
        text = self.contract(*lines, scope="Out of scope:")
        self.check(text, "unparsed",
                   [F("V1", None, "unresolved", self.UNRESOLVED % ("Limits", "ACC-9  an indented line"))],
                   [{"name": "Limits", "lines": lines}])
        self.assertEqual(vh.parse_contract(text)["out_of_scope"], "")
        only = self.contract("Limits: stated on purpose", "ACCEPTED yes", scope="Out of scope:")
        self.check(only, True, [], [{"name": "Limits", "lines": ["Limits: stated on purpose", "ACCEPTED yes"]}])

    def test_the_end_of_a_section_start_word(self):
        for line in ("Limits", "Limits x", "Limits\tx", "Limits, x", "Limits: x"):
            self.check(self.contract(line, "after"), True, [], [{"name": "Limits", "lines": [line, "after"]}])
        for name in ("Definitions", "Evidence requirements", "Coverage list", "Command lines"):
            for end in ("", " x", "\tx", ", x", ": x"):
                self.check(self.contract(name + end), True, [], [{"name": name, "lines": [name + end]}])
        for line in ("Limitsx more", "Limits-x", "limits x", "Limit x", "Evidence requirement x", "Definition",
                     "Coverage  list", "Command line", "Limits\r"):
            self.check(self.contract(line), True, [], None)
        self.check(self.contract("Limits;"), "unparsed",
                   [F("V1", None, "unresolved", "line at column 0 is not a key: 'Limits;'")], None)
        self.check(self.contract(" Limits x"), True, [], None)
        self.assertEqual(vh.parse_contract(self.contract(" Limits x"))["out_of_scope"], "everything else Limits x")

    def test_a_section_start_before_out_of_scope_is_an_ordinary_key(self):
        text = "CON-20261117-01  T\nACC-1  s   Check: sh a.sh\nACC-2  s   Check: sh b.sh\nLimits x\nOut of scope: y\n"
        self.check(text, True, [], None)
        self.assertEqual(vh.parse_contract(text)["extra"], {"Limits": "x"})
        self.assertEqual(vh.validate(text, handoff())["findings"], v1().validate(text, handoff())["findings"])
        no_scope = "CON-20261117-01  T\nACC-1  s   Check: sh a.sh\nACC-2  s   Check: sh b.sh\nLimits x\n"
        self.check(no_scope, True, [], None)

    def test_a_section_runs_to_the_next_start_whatever_the_column(self):
        text = self.contract("Limits x", "Out of scope: again", "Notes  y", "  ACC-1 z", "Definitions", "k")
        sections = [{"name": "Limits", "lines": ["Limits x", "Out of scope: again", "Notes  y", "  ACC-1 z"]},
                    {"name": "Definitions", "lines": ["Definitions", "k"]}]
        self.check(text, "unparsed", [F("V1", None, "unresolved", self.UNRESOLVED % ("Limits", "ACC-1 z"))], sections)
        self.assertEqual(vh.parse_contract(text)["out_of_scope"], "everything else")
        self.assertEqual(vh.parse_contract(text)["extra"], {"sections": sections})

    def test_lines_that_begin_with_acc_or_con(self):
        cases = [
            ("ACC-6  x", "defect"), ("ACC-6", "defect"), ("CON-5", "defect"), ("CON-20261118-99  y", "defect"),
            ("ACC-A", "unresolved"), ("CON-...", "unresolved"), ("ACC-", "unresolved"), ("  ACC-9 x", "unresolved"),
            ("\tCON-1 x", "unresolved"), (" \t ACC-1", "unresolved"),
            ("ACCEPTED yes", None), ("acc-1 x", None), ("ACC1 x", None), ("x ACC-6", None), ("AC-6", None),
            ("-ACC-6", None), ("CONTRACT x", None), ("\u00a0ACC-6", None),
        ]
        for line, kind in cases:
            text = self.contract("Limits x", line)
            if kind == "defect":
                self.check(text, False, [self.DEFECT], [{"name": "Limits", "lines": ["Limits x", line]}])
            elif kind == "unresolved":
                self.check(text, "unparsed",
                           [F("V1", None, "unresolved", self.UNRESOLVED % ("Limits", line.strip(" \t")))],
                           [{"name": "Limits", "lines": ["Limits x", line]}])
            else:
                self.check(text, True, [], [{"name": "Limits", "lines": ["Limits x", line]}])
        long = "\t" + "ACC-" + "z" * 80
        self.check(self.contract("Definitions", long), "unparsed",
                   [F("V1", None, "unresolved", self.UNRESOLVED % ("Definitions", long.strip()[:60]))],
                   [{"name": "Definitions", "lines": ["Definitions", long]}])
        # one message for any number of defect lines; each unresolved line has its own
        text = self.contract("Limits x", "ACC-6 a", "CON-1 b", "ACC-A c", "ACC-B d")
        self.assertEqual(vh.validate(text, handoff())["findings"], [
            self.DEFECT,
            F("V1", None, "unresolved", self.UNRESOLVED % ("Limits", "ACC-A c")),
            F("V1", None, "unresolved", self.UNRESOLVED % ("Limits", "ACC-B d"))])

    def test_the_last_physical_line_counts_with_or_without_a_line_feed(self):
        self.check(CONTRACT + "Limits x", True, [], [{"name": "Limits", "lines": ["Limits x"]}])
        self.check(CONTRACT + "Limits x\n\n", True, [], [{"name": "Limits", "lines": ["Limits x", ""]}])
        self.check(CONTRACT + "Limits x\nACC-6 y", False, [self.DEFECT],
                   [{"name": "Limits", "lines": ["Limits x", "ACC-6 y"]}])
        self.assertEqual(vh.validate(CONTRACT.rstrip("\n"), handoff()), ok_result())
        self.assertEqual(vh.validate(CONTRACT, handoff().rstrip("\n")), ok_result())
        self.assertEqual(vh.parse_handoff(handoff().rstrip("\n"))["values"]["next"], "Accept.")
        self.assertEqual(vh.parse_contract(CONTRACT.rstrip("\n"))["out_of_scope"], "everything else")

    def test_a_key_named_sections_is_not_taken_for_the_list(self):
        plain = self.contract("sections  abc")
        self.assertEqual(vh.validate(plain, handoff()), ok_result())
        self.assertEqual(vh.parse_contract(plain)["extra"], {"sections": "abc"})
        both = self.contract("sections  abc", "Limits x")
        self.check(both, True, [], [{"name": "Limits", "lines": ["Limits x"]}])
        as_json = json.dumps({"id": "CON-20261117-01", "sections": "abc", "ACC-1": "s   Check: sh a.sh",
                              "ACC-2": "s   Check: sh b.sh"})
        self.assertEqual(vh.validate(as_json, handoff()), ok_result())
        self.assertEqual(vh.parse_contract(as_json)["extra"], {"sections": "abc"})

    def test_a_json_contract_has_no_sections(self):
        text = json.dumps({"id": "CON-20261117-01", "ACC-1": "s   Check: sh a.sh", "ACC-2": "s   Check: sh b.sh",
                           "Limits": "x", "out_of_scope": "o"})
        self.assertEqual(vh.validate(text, handoff()), ok_result())
        self.assertEqual(vh.parse_contract(text)["extra"], {"Limits": "x"})

    def test_a_handoff_has_no_sections(self):
        result = vh.validate(CONTRACT, handoff(extra="Limits x\nDefinitions\n"))
        self.assertEqual(result, ok_result())
        self.assertEqual(vh.parse_handoff(handoff(extra="Limits x\n"))["extra"], {"Limits": "x"})

    def test_the_real_contracts_are_frozen(self):
        for name, digest in sorted(FROZEN.items()):
            self.assertEqual(hashlib.sha256((REAL / name).read_bytes()).hexdigest(), digest, name)
        self.assertEqual(sorted(p.name for p in REAL.iterdir()),
                         sorted(list(FROZEN) + ["valid-for-%s.txt" % c for c in REAL_CONTRACTS]))

    def test_the_three_real_contracts_are_valid_with_a_valid_handoff(self):
        expected = {
            "CON-20261118-01": (["Limits", "Definitions", "Command lines", "Evidence requirements"], [2, 29, 3, 8]),
            "CON-20261118-08": (["Limits", "Coverage list", "Definitions", "Evidence requirements"], [2, 15, 9, 6]),
            "CON-20261118-11": (["Limits", "Evidence requirements"], [12, 6]),
        }
        for cid in REAL_CONTRACTS:
            text = (REAL / (cid + ".txt")).read_text(encoding="utf-8")
            hand = (REAL / ("valid-for-" + cid + ".txt")).read_text(encoding="utf-8")
            self.assertEqual(vh.validate(text, hand), ok_result(), cid)
            parsed = vh.parse_contract(text)
            sections = parsed["extra"]["sections"]
            self.assertEqual(([s["name"] for s in sections], [len(s["lines"]) for s in sections]), expected[cid], cid)
            self.assertEqual(parsed["ids"], [cid])
            self.assertEqual(parsed["id"], cid)
            # the sections hold every physical line from the first section start to the end
            physical = text.split("\n")[:-1]
            first = next(i for i, l in enumerate(physical) if l.startswith("Limits"))
            self.assertEqual([l for s in sections for l in s["lines"]], physical[first:], cid)
            self.assertEqual(vh.parse_handoff(hand)["values"]["contract"],
                             " | ".join("%s VERIFIED" % s["id"] for s in parsed["statements"]))
            self.assertTrue(all(s["check"] and s["match"] == s["check"] for s in parsed["statements"]))
        self.assertEqual(vh.parse_contract((REAL / "CON-20261118-08.txt").read_text(encoding="utf-8"))["out_of_scope"],
                         "part 2 to part 5; the modes `off`, `gate` and `each`; any deny of a commit; a release, a push, "
                         "a merge to main and an update of the installed plugin or of the Codex skills, each of which "
                         "needs the owner's GO.")


class ColonKeyTests(unittest.TestCase):
    """D4 and D8: the keys of a handoff that end with a colon."""

    PLAIN_VALUES = vh.parse_handoff(handoff())["values"]

    def check(self, text, valid, findings, contract=CONTRACT, same_as_v1=None):
        result = vh.validate(contract, text)
        self.assertEqual(result, ok_result(valid, findings))
        if same_as_v1 is not None:
            old = v1().validate(contract, same_as_v1)
            self.assertEqual((result["valid"], result["findings"]), (old["valid"], old["findings"]))

    def test_k1_every_key_with_a_colon(self):
        text = colon_handoff("all")
        self.check(text, True, [], same_as_v1=handoff())
        parsed = vh.parse_handoff(text)
        self.assertEqual([k for k, _ in parsed["pairs"]], list(vh.REQUIRED_KEYS))
        self.assertEqual(parsed["values"], self.PLAIN_VALUES)
        self.assertEqual(parsed["problems"], [])
        self.assertEqual(parsed["extra"], {})

    def test_k2_no_key_with_a_colon(self):
        self.assertEqual(colon_handoff(), handoff())
        self.check(colon_handoff(), True, [], same_as_v1=handoff())

    def test_k3_the_two_forms_mixed(self):
        for colon in ({"id", "state", "checks", "contract", "next"}, {"from", "readback", "changes", "open"},
                      {"next"}, {"id"}):
            text = colon_handoff(colon)
            self.check(text, True, [], same_as_v1=handoff())
            self.assertEqual(vh.parse_handoff(text)["values"], self.PLAIN_VALUES, colon)

    def test_k4_done_with_an_unverified_acc(self):
        contract = "ACC-1 VERIFIED | ACC-2 UNVERIFIED (not run)"
        self.check(colon_handoff("all", contract=contract), False,
                   [F("V5", "ACC-2", "defect", "state DONE but ACC-2 is not VERIFIED")],
                   same_as_v1=handoff(contract=contract))

    def test_k5_a_key_is_missing(self):
        for key in ("next", "id", "checks"):
            self.check(colon_handoff("all", drop=(key,)), False,
                       [F("V2", None, "defect", "the handoff has no `%s` key" % key)],
                       same_as_v1=handoff(drop=(key,)))

    def test_k6_a_word_with_a_colon_that_is_no_key(self):
        text = colon_handoff("all", override={"summary": "summary:  Done.\nstatus: ok"})
        self.check(text, "unparsed", [F("V2", None, "unresolved", "line at column 0 is not a key: 'status: ok'")])
        parsed = vh.parse_handoff(text)
        self.assertEqual(parsed["values"]["summary"], "Done.\nstatus: ok")
        self.assertEqual(parsed["extra"], {})
        self.assertEqual([k for k, _ in parsed["pairs"]], list(vh.REQUIRED_KEYS))
        # before the first key there is no field to keep it with
        first = "status: ok\n" + colon_handoff("all")
        self.check(first, "unparsed", [F("V2", None, "unresolved", "line at column 0 is not a key: 'status: ok'")])
        self.assertEqual(vh.parse_handoff(first)["values"], self.PLAIN_VALUES)
        # in a handoff without a colon key the finding is the same, and the line is kept as well
        plain = vh.validate(CONTRACT, handoff(extra="status: ok\n"))
        self.assertEqual(plain["findings"], [F("V2", None, "unresolved", "line at column 0 is not a key: 'status: ok'")])
        self.assertEqual(vh.parse_handoff(handoff(extra="status: ok\n"))["values"]["next"], "Accept.\nstatus: ok")
        self.assertEqual(v1().validate(CONTRACT, handoff(extra="status: ok\n"))["findings"], plain["findings"])

    def test_k7_a_key_with_a_colon_and_no_value(self):
        text = colon_handoff("all", override={"open": "open:"})
        self.check(text, True, [])
        self.assertEqual(vh.parse_handoff(text)["values"]["open"], "")
        text = colon_handoff("all", state="PARTIAL", override={"contract": "contract:"})
        self.check(text, False, [F("V3", "ACC-1", "defect", "ACC-1 has no status"),
                                 F("V3", "ACC-2", "defect", "ACC-2 has no status")])
        # an indented line below an empty key is its value
        text = colon_handoff("all", override={"next": "next:\n   Accept."})
        self.check(text, True, [])
        self.assertEqual(vh.parse_handoff(text)["values"]["next"], "Accept.")

    def test_k8_list_items_at_column_0_below_a_key(self):
        text = colon_handoff("all", override={"changes": "changes:\n- a.py: new\n- b.py: changed"})
        self.check(text, "unparsed", [F("V2", None, "unresolved", "line at column 0 is not a key: '- a.py: new'"),
                                      F("V2", None, "unresolved", "line at column 0 is not a key: '- b.py: changed'")])
        self.assertEqual(vh.parse_handoff(text)["values"]["changes"], "- a.py: new\n- b.py: changed")
        text = colon_handoff("all", override={"changes": "changes: first\n- a.py: new"})
        self.assertEqual(vh.parse_handoff(text)["values"]["changes"], "first\n- a.py: new")

    def test_k8_status_bullets_below_contract_are_read(self):
        text = colon_handoff("all", override={"contract": "contract:\n- ACC-1 VERIFIED\n- ACC-2 VERIFIED (all)"})
        self.check(text, "unparsed", [F("V2", None, "unresolved", "line at column 0 is not a key: '- ACC-1 VERIFIED'"),
                                      F("V2", None, "unresolved", "line at column 0 is not a key: '- ACC-2 VERIFIED (all)'")])
        # a bullet that is missing is a defect, because every line below the key could be read
        text = colon_handoff("all", state="PARTIAL", override={"contract": "contract:\n- ACC-1 VERIFIED"})
        self.check(text, False, [F("V2", None, "unresolved", "line at column 0 is not a key: '- ACC-1 VERIFIED'"),
                                 F("V3", "ACC-2", "defect", "ACC-2 has no status")])
        # a bullet with a bad status or an unknown ACC is a defect
        text = colon_handoff("all", state="PARTIAL", override={"contract": "contract:\n- ACC-1 PASSED\n- ACC-2 VERIFIED\n- ACC-7 FAILED"})
        self.check(text, False, [F("V2", None, "unresolved", "line at column 0 is not a key: '- ACC-1 PASSED'"),
                                 F("V2", None, "unresolved", "line at column 0 is not a key: '- ACC-2 VERIFIED'"),
                                 F("V2", None, "unresolved", "line at column 0 is not a key: '- ACC-7 FAILED'"),
                                 F("V3", "ACC-1", "defect", "status 'PASSED' of ACC-1 is not VERIFIED, FAILED or UNVERIFIED"),
                                 F("V3", "ACC-7", "defect", "ACC-7 is not an ACC of the contract")])
        # a line below contract that is no status makes a missing status unresolved, not a defect
        text = colon_handoff("all", state="PARTIAL", override={"contract": "contract:\n- ACC-1 VERIFIED\nsee the table"})
        self.check(text, "unparsed", [
            F("V2", None, "unresolved", "line at column 0 is not a key: '- ACC-1 VERIFIED'"),
            F("V2", None, "unresolved", "line at column 0 is not a key: 'see the table'"),
            F("V3", None, "unresolved", "the contract status 'see the table' cannot be read as `ACC-<n> <STATUS>`"),
            F("V3", "ACC-2", "unresolved", "ACC-2 has no status")])
        # in the same case with state DONE the V5 finding is unresolved too
        text = colon_handoff("all", override={"contract": "contract:\n- ACC-1 VERIFIED\nsee the table"})
        self.check(text, "unparsed", [
            F("V2", None, "unresolved", "line at column 0 is not a key: '- ACC-1 VERIFIED'"),
            F("V2", None, "unresolved", "line at column 0 is not a key: 'see the table'"),
            F("V3", None, "unresolved", "the contract status 'see the table' cannot be read as `ACC-<n> <STATUS>`"),
            F("V3", "ACC-2", "unresolved", "ACC-2 has no status"),
            F("V5", "ACC-2", "unresolved", "state DONE but ACC-2 has no status")])

    def test_a_kept_line_never_creates_a_defect_below_state(self):
        layout = F("V2", None, "unresolved", "line at column 0 is not a key: '!! x'")
        plain = handoff().replace("state     DONE\n", "state     DONE\n!! x\n")
        colon = colon_handoff("all", override={"state": "state:    DONE\n!! x"})
        for text in (plain, colon):
            self.check(text, "unparsed", [layout], same_as_v1=plain)
            parsed = vh.parse_handoff(text)
            self.assertEqual((parsed["values"]["state"], parsed["own_values"]["state"]), ("DONE\n!! x", "DONE"))
        # a state that is wrong on its own is still a defect, and V5 still reads DONE
        wrong = colon_handoff("all", state="OK", override={"state": "state:    OK\n!! x"})
        self.check(wrong, False, [F("V2", None, "defect", "state 'OK' is not DONE, PARTIAL, BLOCKED or UNABLE"), layout])
        undone = colon_handoff("all", contract="ACC-1 VERIFIED | ACC-2 UNVERIFIED (x)", override={"state": "state:    DONE\n!! x"})
        self.check(undone, False, [layout, F("V5", "ACC-2", "defect", "state DONE but ACC-2 is not VERIFIED")])
        # an indented line after a kept line still continues the value
        both = colon_handoff("all", override={"summary": "summary:  a\n!! x\n    b"})
        self.assertEqual(vh.parse_handoff(both)["own_values"]["summary"], "a\nb")
        self.assertEqual(vh.parse_handoff(both)["values"]["summary"], "a\n!! x\nb")

    def test_a_kept_line_below_checks_counts_as_evidence_but_is_never_a_command(self):
        # a kept line that equals a Check text is no match: V6, and no V4 defect
        text = colon_handoff("all", override={"checks": "checks:   sh a.sh | exit 0 | ok\nsh b.sh"})
        self.check(text, "unparsed", [F("V2", None, "unresolved", "line at column 0 is not a key: 'sh b.sh'"),
                                      F("V6", "ACC-2", "unresolved",
                                        "no checks line has the Check: text of ACC-2 as its command")])
        self.assertEqual(vh.parse_handoff(text)["checks_lines"],
                         [{"text": "sh a.sh | exit 0 | ok", "multiline": False, "kept": False},
                          {"text": "sh b.sh", "multiline": False, "kept": True}])
        # a kept line with complete evidence is still no command: the ACC stays unmatched
        full = colon_handoff("all", override={"checks": "checks:   sh a.sh | exit 0 | ok\nsh b.sh | exit 0 | ok"})
        self.check(full, "unparsed", [F("V2", None, "unresolved", "line at column 0 is not a key: 'sh b.sh | exit 0 | ok'"),
                                      F("V6", "ACC-2", "unresolved",
                                        "no checks line has the Check: text of ACC-2 as its command")])
        # a kept line that reads as evidence by itself (it mentions an exit code) counts: no V4 defect for lack of evidence
        only = colon_handoff("all", override={"checks": "checks:\n- sh a.sh: exit 0, ok\n- sh b.sh: exit 0, ok"})
        self.check(only, "unparsed", [F("V2", None, "unresolved", "line at column 0 is not a key: '- sh a.sh: exit 0, ok'"),
                                      F("V2", None, "unresolved", "line at column 0 is not a key: '- sh b.sh: exit 0, ok'"),
                                      F("V6", "ACC-1", "unresolved", "no checks line has the Check: text of ACC-1 as its command"),
                                      F("V6", "ACC-2", "unresolved", "no checks line has the Check: text of ACC-2 as its command")])
        # a kept line that is no evidence does not hide a missing one: the defect of version 1 stays
        none = handoff(checks=("none",)).replace("checks    none\n", "checks    none\n!! x\n")
        expected = [F("V2", None, "unresolved", "line at column 0 is not a key: '!! x'"),
                    F("V4", "ACC-1", "defect", "ACC-1 has no evidence line in checks"),
                    F("V4", "ACC-2", "defect", "ACC-2 has no evidence line in checks"),
                    F("V6", "ACC-1", "unresolved", "no checks line has the Check: text of ACC-1 as its command"),
                    F("V6", "ACC-2", "unresolved", "no checks line has the Check: text of ACC-2 as its command")]
        self.check(none, False, expected, same_as_v1=none)
        self.assertEqual(vh.parse_handoff(none)["checks_lines"][1], {"text": "!! x", "multiline": False, "kept": True})
        colon = colon_handoff("all", checks=("none",), override={"checks": "checks:   none\n!! x"})
        self.check(colon, False, expected)
        # a mere count of kept lines: the bullets below checks in HND-20261117-47 are kept lines
        kept = [l for l in vh.parse_handoff((REAL / "HND-20261117-47.txt").read_text(encoding="utf-8"))["checks_lines"]]
        self.assertEqual((len(kept), sum(1 for l in kept if l["kept"])), (9, 9))
        # the first checks key is the one that is read, even when it is empty
        first = vh.parse_handoff("checks:\nchecks: sh a.sh | exit 0 | ok\n")
        self.assertEqual((first["checks_lines"], first["own_values"]["checks"], first["values"]["checks"]), ([], "", ""))
        second = vh.parse_handoff("checks: sh a.sh | exit 0 | ok\nchecks:\n")
        self.assertEqual(second["checks_lines"], [{"text": "sh a.sh | exit 0 | ok", "multiline": False, "kept": False}])

    def test_k9_a_value_that_begins_with_a_colon_word(self):
        for line in ("summary: task: x", "summary task: x", "summary:   task: x  ", "summary:\ttask: x\t", "summary\ttask: x"):
            text = colon_handoff("all", override={"summary": line})
            self.check(text, True, [])
            self.assertEqual(vh.parse_handoff(text)["values"]["summary"], "task: x", line)
        text = colon_handoff((), override={"summary": "summary task: x"})
        self.assertEqual(vh.parse_handoff(text)["values"]["summary"], "task: x")
        # no space after the colon is a delimiter all the same
        text = colon_handoff("all", override={"state": "state:DONE"})
        self.check(text, True, [])

    def test_k10_a_doubled_colon(self):
        text = colon_handoff("all", override={"state": "state:: DONE"})
        self.check(text, False, [F("V2", None, "defect", "state ': DONE' is not DONE, PARTIAL, BLOCKED or UNABLE")])
        self.assertEqual(vh.parse_handoff(text)["values"]["state"], ": DONE")
        text = colon_handoff("all", override={"summary": "summary:: x"})
        self.assertEqual(vh.parse_handoff(text)["values"]["summary"], ": x")

    def test_k11_one_key_in_both_forms(self):
        text = colon_handoff("all", extra="summary  Again.\n")
        self.check(text, False, [F("V2", None, "defect", "the key `summary` appears 2 times")],
                   same_as_v1=handoff(extra="summary  Again.\n"))
        text = colon_handoff((), extra="summary: Again.\n")
        self.check(text, False, [F("V2", None, "defect", "the key `summary` appears 2 times")])
        text = colon_handoff("all", extra="summary: Again.\n")
        self.check(text, False, [F("V2", None, "defect", "the key `summary` appears 2 times")])

    def test_k12_a_status_outside_the_three_words(self):
        self.check(colon_handoff("all", state="PARTIAL", contract="ACC-1 PASSED | ACC-2 VERIFIED"), False,
                   [F("V3", "ACC-1", "defect", "status 'PASSED' of ACC-1 is not VERIFIED, FAILED or UNVERIFIED")],
                   same_as_v1=handoff(state="PARTIAL", contract="ACC-1 PASSED | ACC-2 VERIFIED"))
        self.check(colon_handoff("all", contract="ACC-1 PASSED | ACC-2 VERIFIED"), False,
                   [F("V3", "ACC-1", "defect", "status 'PASSED' of ACC-1 is not VERIFIED, FAILED or UNVERIFIED"),
                    F("V5", "ACC-1", "defect", "state DONE but ACC-1 is not VERIFIED")],
                   same_as_v1=handoff(contract="ACC-1 PASSED | ACC-2 VERIFIED"))

    def test_k13_an_acc_without_a_check_in_the_contract(self):
        contract = CONTRACT.replace("ACC-2  The tool shall pass.   Check: sh b.sh", "ACC-2  The tool shall pass.")
        findings = [F("V1", "ACC-2", "defect", "ACC-2 has no Check:")]
        self.check(colon_handoff("all"), False, findings, contract=contract, same_as_v1=handoff())
        self.check(colon_handoff({"id", "contract", "next"}), False, findings, contract=contract, same_as_v1=handoff())

    def test_k14_an_unresolved_layout_line_with_an_established_defect(self):
        text = colon_handoff("all", state="OK", override={"changes": "changes:\n- a.py: new"})
        self.check(text, False, [F("V2", None, "defect", "state 'OK' is not DONE, PARTIAL, BLOCKED or UNABLE"),
                                 F("V2", None, "unresolved", "line at column 0 is not a key: '- a.py: new'")])
        text = colon_handoff("all", drop=("next",), override={"changes": "changes:\n- a.py: new"})
        self.check(text, False, [F("V2", None, "defect", "the handoff has no `next` key"),
                                 F("V2", None, "unresolved", "line at column 0 is not a key: '- a.py: new'")])
        text = colon_handoff("all", contract="ACC-1 VERIFIED | ACC-2 UNVERIFIED (x)", override={"changes": "changes:\n- a.py: new"})
        self.check(text, False, [F("V2", None, "unresolved", "line at column 0 is not a key: '- a.py: new'"),
                                 F("V5", "ACC-2", "defect", "state DONE but ACC-2 is not VERIFIED")])

    def test_a_colon_is_a_delimiter_only_directly_after_a_key_of_3_3(self):
        for line in ("idx: a", "summary-x: a", "Summary: a", "summaryx: a", "sum: a", "id-1: a", "profile: a", "Out of scope: a"):
            text = colon_handoff("all", override={"summary": "summary:  Done.\n" + line})
            self.check(text, "unparsed", [F("V2", None, "unresolved", "line at column 0 is not a key: %r" % line)])
            self.assertEqual(vh.parse_handoff(text)["values"]["summary"], "Done.\n" + line)
        # a space before the colon is no delimiter: the colon begins the value
        text = colon_handoff("all", override={"summary": "summary : x"})
        self.check(text, True, [])
        self.assertEqual(vh.parse_handoff(text)["values"]["summary"], ": x")

    def test_an_extra_key_in_a_handoff_with_a_colon_key_is_not_supported(self):
        plain_extra = handoff(extra="profile    extra value\n          more\n")
        self.assertEqual(vh.parse_handoff(plain_extra)["extra"], {"profile": "extra value\nmore"})
        self.assertEqual(vh.validate(CONTRACT, plain_extra), ok_result())
        text = colon_handoff({"next"}, extra="profile    extra value\n          more\n")
        self.check(text, "unparsed", [F("V2", None, "unresolved", "line at column 0 is not a key: 'profile    extra value'")])
        parsed = vh.parse_handoff(text)
        self.assertEqual(parsed["extra"], {})
        self.assertEqual(parsed["values"]["next"], "Accept.\nprofile    extra value\nmore")
        text = colon_handoff("all", extra="Out of scope: x\n")
        self.check(text, "unparsed", [F("V2", None, "unresolved", "line at column 0 is not a key: 'Out of scope: x'")])

    def test_colon_form_matches_version_1_on_the_fixtures(self):
        fix = ROOT / "checks" / "fixtures" / "validate_handoff"
        contract = (fix / "contract.txt").read_text(encoding="utf-8")
        expected = {"handoff_ok.txt": True, "handoff_cut.txt": False, "handoff_broken.txt": "unparsed"}
        for name, valid in sorted(expected.items()):
            text = (fix / name).read_text(encoding="utf-8")
            new, old = vh.validate(contract, text), v1().validate(contract, text)
            self.assertEqual(new["valid"], valid, name)
            self.assertEqual((new["valid"], new["findings"]), (old["valid"], old["findings"]), name)
        self.assertEqual(old["validator_version"], "1.1.0")
        self.assertEqual(vh.validate(contract, (fix / "handoff_cut.txt").read_text(encoding="utf-8"))["findings"], [
            F("V4", "ACC-2", "defect", "ACC-2 has a status of VERIFIED but the command has no exit code: 'sh b.sh'")])
        self.assertEqual(vh.validate(contract, (fix / "handoff_ok.txt").read_text(encoding="utf-8")), ok_result())

    def test_the_frozen_handoff_of_a_colon_form_gives_unparsed(self):
        contract = (REAL / "CON-20261118-01.txt").read_text(encoding="utf-8")
        text = (REAL / "HND-20261117-47.txt").read_text(encoding="utf-8")
        result = vh.validate(contract, text)
        self.assertEqual(result["valid"], "unparsed")
        self.assertIsNot(result["valid"], False)
        self.assertIsNot(result["valid"], True)
        self.assertEqual(sorted(set((f["rule"], f["level"]) for f in result["findings"])),
                         [("V2", "unresolved"), ("V6", "unresolved")])
        self.assertEqual([f for f in result["findings"] if f["rule"] == "V6"],
                         [F("V6", "ACC-%d" % n, "unresolved",
                            "no checks line has the Check: text of ACC-%d as its command" % n) for n in range(1, 6)])
        self.assertEqual(len([f for f in result["findings"] if f["rule"] == "V2"]), 91)
        self.assertTrue(all(f["message"].startswith("line at column 0 is not a key: ")
                            for f in result["findings"] if f["rule"] == "V2"))
        parsed = vh.parse_handoff(text)
        self.assertEqual([k for k, _ in parsed["pairs"]], list(vh.REQUIRED_KEYS))
        self.assertEqual(parsed["extra"], {})
        self.assertEqual(len(parsed["values"]["contract"].split("\n")), 5)
        self.assertEqual(len(parsed["values"]["checks"].split("\n")), 9)

    def test_the_real_handoffs_are_valid_in_every_form(self):
        for cid in REAL_CONTRACTS:
            text = (REAL / (cid + ".txt")).read_text(encoding="utf-8")
            plain = (REAL / ("valid-for-" + cid + ".txt")).read_text(encoding="utf-8")
            as_colon = "\n".join(re.sub(r"^([a-z]+) +", r"\1: ", l) for l in plain.split("\n"))
            self.assertNotEqual(as_colon, plain)
            self.assertEqual(vh.parse_handoff(as_colon)["values"], vh.parse_handoff(plain)["values"], cid)
            mixed = "\n".join(re.sub(r"^([a-z]+) +", r"\1: ", l) if i % 2 else l for i, l in enumerate(plain.split("\n")))
            for form in (plain, as_colon, mixed):
                self.assertEqual(vh.validate(text, form), ok_result(), cid)
                old = v1().validate(text, plain)
                self.assertEqual(old["valid"], "unparsed")  # version 1 cannot read the sections of a real contract


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
        return {p.relative_to(self.FIX).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.FIX.rglob("*")) if p.is_file()}

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
