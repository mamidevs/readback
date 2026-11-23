#!/usr/bin/env python3
"""Offline checks for the record frame (CON-20261123-04).

A stored record holds the publisher's envelope line, the body and the lead's lines after it.
Run: python3 -m unittest discover -s checks -p 'test_record_frame.py' -v
Every framed case is compared with the same body without its frame. Fixtures are synthetic.
"""

import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "checks" / "fixtures" / "record_frame"
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vh = _load("validate_handoff_frame", "scripts/validate_handoff.py")
jr = _load("judge_rounds_frame", "scripts/judge_rounds.py")

ENVELOPE = '<!-- readback project=demo-00000000 scope="shared" -->\n'
CONTRACT = (FIX / "contract.txt").read_text()
SPACED = (FIX / "handoff.txt").read_text()
COLON = (FIX / "handoff-colon.txt").read_text()
JSON_BODY = (FIX / "handoff.json").read_text()
LEAD = (FIX / "lead-lines.txt").read_text()
MARKER = "<!-- readback lead -->"


class FrameTests(unittest.TestCase):
    def same(self, framed_contract, framed_handoff, bare_contract, bare_handoff, valid=True):
        framed = vh.validate(framed_contract, framed_handoff)
        bare = vh.validate(bare_contract, bare_handoff)
        self.assertEqual(framed["valid"], bare["valid"])
        self.assertEqual(framed["findings"], bare["findings"])
        self.assertEqual(bare["valid"], valid, bare["findings"])

    def test_the_bare_fixtures_are_valid(self):
        for body in (SPACED, COLON, JSON_BODY):
            with self.subTest(body=body[:20]):
                self.assertIs(vh.validate(CONTRACT, body)["valid"], True)

    # ACC-1
    def test_a01_an_enveloped_contract_gives_the_result_of_the_bare_contract(self):
        self.same(ENVELOPE + CONTRACT, SPACED, CONTRACT, SPACED)

    def test_a01_an_enveloped_contract_after_blank_lines(self):
        self.same("\n  \n" + ENVELOPE + CONTRACT, SPACED, CONTRACT, SPACED)

    def test_a01_an_enveloped_text_handoff_gives_the_result_of_the_bare_handoff(self):
        self.same(CONTRACT, ENVELOPE + SPACED, CONTRACT, SPACED)

    def test_a01_an_enveloped_colon_handoff_gives_the_result_of_the_bare_handoff(self):
        self.same(CONTRACT, ENVELOPE + COLON, CONTRACT, COLON)

    def test_a01_an_enveloped_json_handoff_gives_the_result_of_the_bare_handoff(self):
        self.same(CONTRACT, ENVELOPE + JSON_BODY, CONTRACT, JSON_BODY)

    def test_a01_a_broken_body_keeps_its_findings_under_the_envelope(self):
        bad = SPACED.replace("state     DONE", "state     MAYBE")
        self.same(CONTRACT, ENVELOPE + bad, CONTRACT, bad, valid=False)

    # ACC-2: the lead marker
    def test_b02_a_json_handoff_with_the_marker_and_lead_lines(self):
        self.same(CONTRACT, JSON_BODY + LEAD, CONTRACT, JSON_BODY)

    def test_b02_an_enveloped_json_handoff_with_the_marker_and_lead_lines(self):
        self.same(CONTRACT, ENVELOPE + JSON_BODY + LEAD, CONTRACT, JSON_BODY)

    def test_b02_a_spaced_key_handoff_with_the_marker_and_lead_lines(self):
        self.same(CONTRACT, SPACED + LEAD, CONTRACT, SPACED)
        self.same(CONTRACT, ENVELOPE + SPACED + LEAD, CONTRACT, SPACED)

    def test_b02_a_colon_key_handoff_with_the_marker_and_lead_lines(self):
        self.same(CONTRACT, COLON + LEAD, CONTRACT, COLON)
        self.same(CONTRACT, ENVELOPE + COLON + LEAD, CONTRACT, COLON)

    def test_b02_the_lines_after_the_marker_are_not_read_whatever_they_hold(self):
        tail = "\n" + MARKER + "\nnext      Changed.\nstate     UNABLE\nsome line\n  indented\n{\n"
        self.same(CONTRACT, SPACED + tail, CONTRACT, SPACED)
        self.same(CONTRACT, COLON + tail, CONTRACT, COLON)
        self.same(CONTRACT, JSON_BODY + tail, CONTRACT, JSON_BODY)

    def test_b02_the_marker_may_be_followed_by_spaces_and_tabs(self):
        self.same(CONTRACT, SPACED + MARKER + " \t \nstate X\n", CONTRACT, SPACED)

    def test_b02_the_marker_at_the_end_without_a_newline(self):
        self.same(CONTRACT, SPACED + MARKER, CONTRACT, SPACED)

    def test_b02_a_second_marker_line_belongs_to_the_ignored_part(self):
        self.same(CONTRACT, SPACED + MARKER + "\nJUDGE x\n" + MARKER + "\nnext Z\n", CONTRACT, SPACED)

    def test_b02_the_marker_text_in_an_indented_continuation_line_ends_nothing(self):
        for body in (SPACED, COLON):
            with self.subTest(body=body[:10]):
                kept = body.replace("Done.", "Done.\n          " + MARKER, 1)
                parsed = vh.parse_handoff(kept)
                self.assertIn(MARKER, parsed["values"]["summary"])
                self.assertEqual(parsed["values"]["next"], "Accept.")
                self.assertEqual(vh.validate(CONTRACT, kept), vh.validate(CONTRACT, body))

    def test_b02_the_marker_text_after_other_text_on_a_line_ends_nothing(self):
        kept = SPACED.replace("Done.", "see " + MARKER, 1)
        self.assertIn(MARKER, vh.parse_handoff(kept)["values"]["summary"])
        self.assertEqual(vh.parse_handoff(kept)["values"]["next"], "Accept.")

    def test_b02_the_marker_text_inside_a_json_string_ends_nothing(self):
        for form in ("string", "escaped newline"):
            with self.subTest(form=form):
                text = "x " + MARKER if form == "string" else "x\n" + MARKER + "\ny"
                data = json.loads(JSON_BODY)
                data["summary"] = text
                body = json.dumps(data, indent=2) + "\n"
                self.assertEqual(vh.parse_handoff(body)["values"]["summary"], text)
                self.assertEqual(vh.parse_handoff(body)["values"]["next"], "Accept.")
                self.assertEqual(vh.validate(CONTRACT, body)["valid"], True)

    def test_b02_the_marker_is_not_an_envelope(self):
        self.same(CONTRACT, MARKER + "\n" + SPACED, CONTRACT, "", valid="unparsed")

    # ACC-3: no marker, the result of main 3d6b4c3
    def test_b03_a_json_handoff_followed_by_text_without_the_marker_is_reported_as_today(self):
        for tail in (LEAD.replace(MARKER + "\n", ""), " trailing words\n", json.dumps({"id": "x"}) + "\n"):
            with self.subTest(tail=tail[:20]):
                result = vh.validate(CONTRACT, JSON_BODY + tail)
                self.assertEqual(result["valid"], "unparsed")
                self.assertEqual([(f["rule"], f["level"]) for f in result["findings"]], [("V2", "unresolved")])
                self.assertTrue(result["findings"][0]["message"].startswith("invalid JSON: Extra data"))

    def test_b03_a_judge_line_before_changes_without_the_marker_is_valid_as_today(self):
        text = SPACED.replace("changes   none\n", "JUDGE notes\nchanges   none\n")
        self.assertNotEqual(text, SPACED)
        result = vh.validate(CONTRACT, text)
        self.assertIs(result["valid"], True)
        self.assertEqual(result["findings"], [])
        self.assertEqual(vh.parse_handoff(text)["extra"], {"JUDGE": "notes"})

    def test_b03_a_judge_line_then_a_second_state_without_the_marker_is_invalid_as_today(self):
        text = SPACED + "JUDGE notes\nstate BLOCKED\n"
        result = vh.validate(CONTRACT, text)
        self.assertIs(result["valid"], False)
        self.assertEqual([(f["rule"], f["level"]) for f in result["findings"]], [("V2", "defect")])

    def test_b03_the_second_case_with_the_marker_before_the_judge_line_is_valid_by_design(self):
        text = SPACED + MARKER + "\nJUDGE notes\nstate BLOCKED\n"
        result = vh.validate(CONTRACT, text)
        self.assertIs(result["valid"], True)
        self.assertEqual(result, vh.validate(CONTRACT, SPACED))

    def test_b03_the_first_case_with_the_marker_before_the_judge_line_loses_the_later_keys(self):
        text = SPACED.replace("changes   none\n", MARKER + "\nJUDGE notes\nchanges   none\n")
        self.assertIs(vh.validate(CONTRACT, text)["valid"], False)

    def test_b03_a_contract_is_read_past_a_lead_marker_line_as_at_main(self):
        # At main 3d6b4c3 the marker line is a column-0 line that is no key (V1 unresolved) and the
        # ACC-3 line below it is read, so the handoff that lacks a status for ACC-3 has two defects.
        contract = CONTRACT + MARKER + "\nACC-3  The tool shall stay.   Check: sh c.sh\n"
        result = vh.validate(contract, SPACED)
        self.assertIs(result["valid"], False)
        self.assertEqual(
            [(f["rule"], f["acc"], f["level"], f["message"]) for f in result["findings"]],
            [("V1", None, "unresolved", "line at column 0 is not a key: %r" % MARKER),
             ("V3", "ACC-3", "defect", "ACC-3 has no status"),
             ("V5", "ACC-3", "defect", "state DONE but ACC-3 has no status")])
        self.assertEqual(vh.parse_contract(contract)["statements"][-1]["id"], "ACC-3")

    # envelope outside the first line
    def test_a05_a_second_envelope_line_is_reported_as_today(self):
        result = vh.validate(CONTRACT, ENVELOPE + ENVELOPE + SPACED)
        self.assertEqual(result["valid"], "unparsed")
        self.assertTrue(any("line at column 0 is not a key" in f["message"] for f in result["findings"]))

    def test_a05_an_envelope_line_that_is_not_the_first_line_is_reported_as_today(self):
        for text in ("id        HND-20261117-01\n" + ENVELOPE + SPACED[SPACED.index("from"):],):
            result = vh.validate(CONTRACT, text)
            self.assertEqual(result["valid"], "unparsed")
            self.assertTrue(any("line at column 0 is not a key" in f["message"] for f in result["findings"]))

    def test_a05_a_second_envelope_line_in_a_contract_is_reported_as_today(self):
        result = vh.validate(ENVELOPE + ENVELOPE + CONTRACT, SPACED)
        self.assertEqual(result["valid"], "unparsed")
        self.assertTrue(any(f["rule"] == "V1" for f in result["findings"]))

    def test_a05_a_line_that_only_starts_like_an_envelope_is_not_one(self):
        for line in ("<!-- readback x\n", "<!-- other x -->\n", "<!--readback x -->\n"):
            with self.subTest(line=line):
                self.assertEqual(vh.validate(CONTRACT, line + SPACED)["valid"], "unparsed")

    def test_a05_a_text_without_frame_gives_the_same_result_as_before(self):
        self.assertEqual(vh.validate(CONTRACT, SPACED),
                         {"valid": True, "findings": [], "validator_version": vh.VALIDATOR_VERSION})
        self.assertEqual(vh.VALIDATOR_VERSION, "2.3.0")


class LineageTests(unittest.TestCase):
    def test_a04_lineage_of_a_contract_with_an_envelope(self):
        self.assertEqual(jr.lineage_of(ENVELOPE + CONTRACT), "CON-20261117-01")

    def test_a04_lineage_of_a_contract_with_blank_lines_around_the_envelope(self):
        self.assertEqual(jr.lineage_of("\n \n" + ENVELOPE + "\n  \n" + CONTRACT), "CON-20261117-01")

    def test_a04_the_lead_marker_is_not_an_envelope(self):
        self.assertIsNone(jr.lineage_of(MARKER + "\n" + CONTRACT))

    def test_a04_lineage_of_a_contract_without_an_envelope(self):
        self.assertEqual(jr.lineage_of(CONTRACT), "CON-20261117-01")

    def test_a04_an_envelope_followed_by_a_line_without_a_con_id_gives_none(self):
        self.assertIsNone(jr.lineage_of(ENVELOPE + "Title\n" + CONTRACT))

    def test_a04_an_envelope_alone_gives_none(self):
        self.assertIsNone(jr.lineage_of(ENVELOPE))

    def test_a04_a_second_envelope_line_gives_none(self):
        self.assertIsNone(jr.lineage_of(ENVELOPE + ENVELOPE + CONTRACT))

    def test_a04_an_envelope_line_that_is_not_the_first_gives_none(self):
        self.assertIsNone(jr.lineage_of("Title\n" + ENVELOPE + CONTRACT))


if __name__ == "__main__":
    unittest.main()
