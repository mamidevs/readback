#!/usr/bin/env python3
"""Offline checks for the skeleton texts of CON-20261123-06.

Run: python3 -m unittest discover -s checks -p 'test_texts_skeleton.py' -v
"""

import os
from pathlib import Path
import re
import unittest

ROOT = Path(os.environ.get("SKELETON_ROOT") or Path(__file__).resolve().parent.parent)

S1 = "Write one command in each Check. Join commands that belong together with `sh -c`."
S2 = "Run `scripts/handoff_skeleton.py` on the contract and put the skeleton that it prints into the worker's prompt."
S3 = "Write the line `<!-- readback lead -->` above the JUDGE line of a stored handoff."
S4 = "Send the results of the joined run to the worker before the judge call, and judge the updated handoff."
S5 = "When the lead gives a skeleton, fill it and return it as your whole report."
JSON_LINE = "Use JSON with the same keys when a script checks it."


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def section(text, heading):
    match = re.search(r"^#+ " + re.escape(heading) + r"[^\n]*\n(.*?)(?=^#+ |\Z)", text, re.S | re.M)
    assert match, heading
    return match.group(1)


def lines_of(rel, heading):
    return section(read(rel), heading).split("\n")


class SentenceTests(unittest.TestCase):
    def one_line(self, rel, heading, sentence):
        lines = lines_of(rel, heading)
        self.assertEqual(lines.count(sentence), 1, "%s %s: %s" % (rel, heading, sentence))
        return lines.index(sentence)

    def test_sentence_1_and_2_in_contract_sections(self):
        for rel, heading in (("output-styles/readback.md", "CONTRACT"), ("PROTOCOL.md", "3.2 CONTRACT")):
            with self.subTest(rel):
                self.assertLess(self.one_line(rel, heading, S1), self.one_line(rel, heading, S2))

    def test_sentence_1_and_2_follow_the_dispatch_sentences(self):
        for rel, heading in (("output-styles/readback.md", "CONTRACT"), ("PROTOCOL.md", "3.2 CONTRACT")):
            with self.subTest(rel):
                lines = lines_of(rel, heading)
                self.assertLess(lines.index("A contract id alone delivers no statement."), lines.index(S1))

    def test_sentence_3_in_accept_sections(self):
        self.one_line("output-styles/readback.md", "ACCEPT", S3)
        self.one_line("PROTOCOL.md", "4. Independence", S3)

    def test_sentence_3_stands_with_the_judge_line_rules(self):
        for rel, heading in (("output-styles/readback.md", "ACCEPT"), ("PROTOCOL.md", "4. Independence")):
            with self.subTest(rel):
                text = "\n".join(lines_of(rel, heading))
                self.assertIn("JUDGE     <PASS, FAIL or none>", text)

    def test_sentence_4_in_accept_sections(self):
        self.one_line("output-styles/readback.md", "ACCEPT", S4)
        self.one_line("PROTOCOL.md", "4. Independence", S4)

    def test_sentence_5_in_when_you_stop(self):
        self.one_line("agents/worker.md", "When you stop", S5)

    def test_existing_json_sentence_stays(self):
        self.assertEqual(lines_of("agents/worker.md", "When you stop").count(
            "The handoff has these keys in this order. " + JSON_LINE), 1)
        self.assertIn(JSON_LINE, read("agents/worker.md"))
        self.assertIn(JSON_LINE, read("PROTOCOL.md"))

    def test_sentences_stay_inside_the_word_limit(self):
        for sentence in (S1, S2, S3, S4, S5):
            self.assertLessEqual(len(sentence.split()), 20, sentence)


class PortableTests(unittest.TestCase):
    def test_portable_skills_hold_the_sentences(self):
        lead = read("portable/skills/readback/SKILL.md")
        for sentence in (S1, S2, S3, S4):
            self.assertIn(sentence, lead)
        self.assertIn(S5, read("portable/skills/readback-worker/SKILL.md"))

    def test_portable_lead_skill_holds_the_script(self):
        self.assertEqual(read("portable/skills/readback/scripts/handoff_skeleton.py"),
                         read("scripts/handoff_skeleton.py"))


class AmendmentTests(unittest.TestCase):
    def test_amendment_13(self):
        text = read("AMENDMENT-13.md")
        self.assertIn("DEC-20261123-07", text)
        self.assertIn("No package ships this amendment yet", text)
        self.assertIn("scripts/handoff_skeleton.py", text)
        self.assertIn("six of six handoffs written without a skeleton gave `unparsed`", text.replace("Six", "six", 1))
        self.assertIn("two spaces before each line", text)
        self.assertIn("fixes the form of a handoff, not its truth", text)
        self.assertIn("wrong escape", text)


if __name__ == "__main__":
    unittest.main()
