#!/usr/bin/env python3
"""Offline checks for the closure-note texts of CON-20261123-08.

Run: python3 -m unittest discover -s checks -p 'test_texts_closure.py' -v
"""

import os
from pathlib import Path
import re
import unittest

ROOT = Path(os.environ.get("CLOSURE_ROOT") or Path(__file__).resolve().parent.parent)

S1 = "Store the handoff when it arrives. Do not change the worker's text and add no line below it."
S2 = "Store each judge call as a review note and the acceptance as an acceptance note. `scripts/closure_note.py` prints both."
S3 = "An older stored handoff holds the lead's lines below the worker's text."
S4 = "The review note holds its JUDGE, execution and receipt lines."
MARKER = "Write the line `<!-- readback lead -->` above the JUDGE line of a stored handoff."
OLD1 = "Add two lines below the stored handoff. Do not change the worker's text."
OLD1_PROTOCOL = "The lead adds two lines below the stored handoff. It does not change the worker's text."
OLD4 = "Store its JUDGE, execution and receipt lines below the handoff."
OLD4_PROTOCOL = "Before ACCEPTED, copy the JUDGE, execution and receipt lines into the handoff."
OLD4_SKILL = ("Keep the JUDGE, execution and receipt lines below the handoff.", "Add ACCEPTED below the original worker text.")

STYLE = ("output-styles/readback.md", "ACCEPT")
PROTOCOL = ("PROTOCOL.md", "4. Independence")
SKILL = ("skills/outside-judge/SKILL.md", None)


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def section(text, heading):
    match = re.search(r"^#+ " + re.escape(heading) + r"[^\n]*\n(.*?)(?=^#+ |\Z)", text, re.S | re.M)
    assert match, heading
    return match.group(1)


def lines_of(rel, heading):
    return section(read(rel), heading).split("\n")


class SentenceTests(unittest.TestCase):
    def one_line(self, where, sentence):
        lines = lines_of(*where)
        self.assertEqual(lines.count(sentence), 1, "%s: %s" % (where, sentence))
        return lines.index(sentence)

    def test_sentence_1_in_accept_sections(self):
        for where in (STYLE, PROTOCOL):
            with self.subTest(where):
                self.one_line(where, S1)

    def test_sentence_2_in_accept_sections(self):
        for where in (STYLE, PROTOCOL):
            with self.subTest(where):
                self.one_line(where, S2)

    def test_sentence_2_stands_directly_after_sentence_1(self):
        for where in (STYLE, PROTOCOL):
            with self.subTest(where):
                self.assertEqual(self.one_line(where, S2), self.one_line(where, S1) + 1)

    def test_sentence_3_in_accept_sections(self):
        for where in (STYLE, PROTOCOL):
            with self.subTest(where):
                self.one_line(where, S3)

    def test_sentence_3_stands_directly_above_the_marker_sentence(self):
        for where in (STYLE, PROTOCOL):
            with self.subTest(where):
                self.assertEqual(self.one_line(where, MARKER), self.one_line(where, S3) + 1)

    def test_marker_sentence_stays_word_for_word(self):
        for where in (STYLE, PROTOCOL):
            with self.subTest(where):
                self.one_line(where, MARKER)

    def test_sentence_4_in_the_style_ends_the_second_sentence_of_its_line(self):
        lines = lines_of(*STYLE)
        hits = [l for l in lines if S4 in l]
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].startswith("Read the full answer. " + S4 + " Summarize any finding"))

    def test_sentence_4_in_protocol(self):
        hits = [l for l in lines_of(*PROTOCOL) if S4 in l]
        self.assertEqual(len(hits), 1)

    def test_sentence_4_in_step_4_of_the_outside_judge_skill(self):
        steps = [l for l in read(SKILL[0]).split("\n") if l.startswith("4. ")]
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0].startswith("4. " + S4 + " Summarize any finding needed for ACCEPTED"))
        self.assertNotIn("below", steps[0])

    def test_sentences_stay_inside_the_word_limit(self):
        for sentence in (S1.split(". ")[0] + ".", S1.split(". ")[1], S3, S4, MARKER):
            self.assertLessEqual(len(sentence.split()), 20, sentence)
        for part in S2.split(". "):
            self.assertLessEqual(len(part.split()), 20, part)


class ReplacedTests(unittest.TestCase):
    FILES = ("output-styles/readback.md", "PROTOCOL.md", "skills/outside-judge/SKILL.md",
             "portable/skills/readback/SKILL.md")

    def test_replaced_sentences_are_gone(self):
        gone = (OLD1, OLD1_PROTOCOL, OLD4, OLD4_PROTOCOL) + OLD4_SKILL
        for rel in self.FILES:
            text = read(rel)
            for sentence in gone:
                with self.subTest(rel=rel, sentence=sentence):
                    self.assertNotIn(sentence, text)

    def test_the_other_judge_lines_rules_stay(self):
        for rel in ("output-styles/readback.md", "PROTOCOL.md"):
            self.assertIn("Copy the helper's complete JUDGE line; ACCEPTED records what the acceptance rests on.", read(rel))


class PortableTests(unittest.TestCase):
    def test_portable_lead_skill_holds_sentences_1_to_4_in_the_accept_section(self):
        lead = read("portable/skills/readback/SKILL.md")
        for sentence in (S1, S2, S3, MARKER):
            self.assertEqual(lead.split("\n").count(sentence), 1, sentence)
        self.assertIn("Read the full answer. " + S4, lead)

    def test_portable_lead_skill_holds_the_script(self):
        self.assertEqual(read("portable/skills/readback/scripts/closure_note.py"), read("scripts/closure_note.py"))

    def test_sync_lists_the_script(self):
        self.assertIn('"closure_note.py"', read("scripts/sync.py"))


class AmendmentTests(unittest.TestCase):
    def test_amendment_14(self):
        text = read("AMENDMENT-14.md")
        self.assertIn("DEC-20261123-07", text)
        self.assertIn("No package ships this amendment yet", text)
        self.assertIn("scripts/closure_note.py", text)
        self.assertIn("11 handoffs unstored until a verdict was known", text)
        self.assertIn("a stored record cannot get lines later", text)
        self.assertIn("Five replacement chains were recorded as prose", text)
        self.assertIn("On 2026-11-19 another project stored verdicts in free-form notes", text)
        self.assertIn("unjudged handoffs", text)
        self.assertIn("It does not show that the review exists, that the judge said PASS, or that the acceptance is justified.", text)


if __name__ == "__main__":
    unittest.main()
