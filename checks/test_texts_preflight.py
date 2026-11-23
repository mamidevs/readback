#!/usr/bin/env python3
"""Offline checks for the record-preflight texts of CON-20261123-10.

Run: python3 -m unittest discover -s checks -p 'test_texts_preflight.py' -v
"""

import os
from pathlib import Path
import re
import unittest

ROOT = Path(os.environ.get("PREFLIGHT_ROOT") or Path(__file__).resolve().parent.parent)

HEADING = "Project settings: resolve the record destination first"
BEFORE = "All record, LOG and CURRENT writes go through this publisher."
SENTENCE = "Run `scripts/record_preflight.py` on a request before you publish it. It reports and refuses nothing."
SYNC_TUPLE_FIRST = ('for helper in ("project_registry.py", "resolve_codex_project.py", "resolve_claude_project.py", '
                    '"project_index.py", "publish_records.py", "record_preflight.py", "task_current.py",')
SYNC_TUPLE_SECOND = ('"review_receipt.py", "review_capture.py", "review_redaction.py", "review_retention.py", '
                     '"validate_handoff.py", "commit_observation.py",')
SYNC_TUPLE_THIRD = '"readback_settings.py", "readback_session.py", "judge_rounds.py", "handoff_skeleton.py", "closure_note.py"'

STYLE = "output-styles/readback.md"
PORTABLE = "portable/skills/readback/SKILL.md"


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def section(text, heading):
    match = re.search(r"^#+ " + re.escape(heading) + r"[^\n]*\n(.*?)(?=^#+ |\Z)", text, re.S | re.M)
    assert match, heading
    return match.group(1)


class SentenceTests(unittest.TestCase):
    def lines(self, rel):
        return section(read(rel), HEADING).split("\n")

    def test_the_sentence_stands_once_in_the_named_section_of_each_file(self):
        for rel in (STYLE, PORTABLE):
            with self.subTest(rel):
                self.assertEqual(self.lines(rel).count(SENTENCE), 1)

    def test_the_sentence_stands_directly_after_the_publisher_sentence(self):
        for rel in (STYLE, PORTABLE):
            with self.subTest(rel):
                lines = self.lines(rel)
                self.assertEqual(lines.count(BEFORE), 1)
                self.assertEqual(lines.index(SENTENCE), lines.index(BEFORE) + 1)

    def test_the_sentence_stands_nowhere_else_in_the_file(self):
        for rel in (STYLE, PORTABLE):
            with self.subTest(rel):
                self.assertEqual(read(rel).count(SENTENCE), 1)

    def test_each_sentence_stays_inside_the_word_limit(self):
        for part in SENTENCE.split(". "):
            self.assertLessEqual(len(part.split()), 20, part)

    def test_the_publisher_sentence_stays_word_for_word(self):
        for rel in (STYLE, PORTABLE):
            with self.subTest(rel):
                self.assertIn(BEFORE + "\n", read(rel))


class SyncTests(unittest.TestCase):
    def test_sync_copies_the_script_directly_after_publish_records(self):
        first = [l for l in read("scripts/sync.py").split("\n") if l.startswith("    for helper in (")]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].strip(), SYNC_TUPLE_FIRST)

    def test_the_second_and_third_line_of_the_tuple_stay(self):
        lines = [l.strip() for l in read("scripts/sync.py").split("\n")]
        at = lines.index(SYNC_TUPLE_FIRST)
        self.assertEqual(lines[at + 1], SYNC_TUPLE_SECOND)
        self.assertTrue(lines[at + 2].startswith(SYNC_TUPLE_THIRD), lines[at + 2])

    def test_the_portable_lead_skill_holds_the_script(self):
        self.assertEqual(read("portable/skills/readback/scripts/record_preflight.py"),
                         read("scripts/record_preflight.py"))


class AmendmentTests(unittest.TestCase):
    def setUp(self):
        self.text = read("AMENDMENT-16.md")

    def test_amendment_16_names_the_script_and_the_state(self):
        self.assertIn("# Amendment 16", self.text)
        self.assertIn("CON-20261123-10", self.text)
        self.assertIn("No package ships this amendment yet", self.text)
        self.assertIn("scripts/record_preflight.py", self.text)
        self.assertIn("scripts/sync.py", self.text)

    def test_amendment_16_holds_the_measured_reasons(self):
        self.assertIn("on 2026-11-23 the lead paired every handoff with its contract by hand and ran the validator by hand", self.text)
        self.assertIn("it stored 18 handoffs that day", self.text)
        self.assertIn("a handoff did not name its contract in a key before validator 2.3.0", self.text)
        self.assertIn("Codex reviewed the place of the check", self.text)
        self.assertIn("inside the publisher it adds a recovery path and lock time for a result that refuses nothing", self.text)
        self.assertIn("no skipped check and no changed retry result was measured", self.text)

    def test_amendment_16_holds_the_limits(self):
        self.assertIn("The script reports and refuses nothing", self.text)
        self.assertIn("An exit status 0 does not show that a verdict is right or that an acceptance is justified", self.text)
        self.assertIn("Nothing makes the lead run it", self.text)

    def test_amendment_16_names_the_sentence_and_the_tests(self):
        self.assertIn(SENTENCE, self.text)
        self.assertIn("checks/test_record_preflight.py", self.text)
        self.assertIn("checks/test_texts_preflight.py", self.text)


if __name__ == "__main__":
    unittest.main()
