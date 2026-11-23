#!/usr/bin/env python3
"""Offline checks for the host-refusal texts of CON-20261123-12.

Run: python3 -m unittest discover -s checks -p 'test_texts_refusal.py' -v
"""

import os
from pathlib import Path
import re
import unittest

ROOT = Path(os.environ.get("REFUSAL_ROOT") or Path(__file__).resolve().parent.parent)

SENTENCE = ("When the host refuses the judge call before the helper starts, write "
            "`JUDGE none | <provider:model> | <repo@sha or none> | no verdict: host refusal: <the host's reason>`. "
            "Do not send the content another way. Ask the owner for a DECISION.")
STYLE = "output-styles/readback.md"
PROTOCOL = "PROTOCOL.md"
SKILL = "skills/outside-judge/SKILL.md"
PORTABLE = "portable/skills/readback/SKILL.md"
STYLE_BEFORE = "A call that fails gives no verdict. Write `JUDGE none` and do not accept the work."
PROTOCOL_BEFORE = "- A call that fails gives no verdict. Write `JUDGE none`. The lead does not accept the work."
SKILL_BEFORE_START = "- Exit code 3 means that the call gave no verdict."
HEADING = "### When the host refuses the judge call"
ABOVE = "### Judge line for Claude Code"
OWNER_SENTENCE = "The owner decides whether to authorize this export; the host must also permit the call."


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def lines(rel):
    return read(rel).split("\n")


class RuleTextTests(unittest.TestCase):
    def test_the_sentence_stands_once_directly_after_the_no_verdict_rule(self):
        for rel, before in ((STYLE, STYLE_BEFORE), (PORTABLE, STYLE_BEFORE)):
            with self.subTest(rel):
                ls = lines(rel)
                self.assertEqual(ls.count(SENTENCE), 1)
                self.assertEqual(ls.count(before), 1)
                self.assertEqual(ls.index(SENTENCE), ls.index(before) + 1)
                self.assertEqual(read(rel).count(SENTENCE), 1)

    def test_protocol_holds_the_sentence_as_a_list_item_after_its_rule(self):
        ls = lines(PROTOCOL)
        self.assertEqual(ls.count(PROTOCOL_BEFORE), 1)
        self.assertEqual(ls.index("- " + SENTENCE), ls.index(PROTOCOL_BEFORE) + 1)
        self.assertEqual(read(PROTOCOL).count(SENTENCE), 1)

    def test_skill_holds_the_sentence_as_a_list_item_after_exit_code_3(self):
        ls = lines(SKILL)
        at = [i for i, l in enumerate(ls) if l.startswith(SKILL_BEFORE_START)]
        self.assertEqual(len(at), 1)
        self.assertEqual(ls[at[0] + 1], "- " + SENTENCE)
        self.assertEqual(read(SKILL).count(SENTENCE), 1)


class InstallTests(unittest.TestCase):
    def section(self):
        ls = lines("INSTALL.md")
        self.assertEqual(ls.count(HEADING), 1)
        start = ls.index(HEADING)
        end = start + 1
        while not ls[end].startswith("#"):
            end += 1
        return ls, start, end

    def test_the_heading_stands_directly_above_the_judge_line_heading(self):
        ls, start, end = self.section()
        self.assertEqual(ls[end], ABOVE)
        auto = ls.index("## Automatic outside judge")
        self.assertLess(auto, start)
        self.assertFalse([l for l in ls[auto + 1:start] if l.startswith("## ")])

    def test_the_section_quotes_the_owner_sentence(self):
        ls, start, end = self.section()
        self.assertIn(OWNER_SENTENCE, "\n".join(ls[start:end]))

    def test_the_section_holds_no_command_and_no_switch_off_word(self):
        ls, start, end = self.section()
        body = ls[start + 1:end]
        for line in body:
            self.assertFalse(re.match(r"\s*(```|\$|python|bash|sh |git |codex |claude )", line), line)
        for word in ("bypass", "disable", "skip"):
            self.assertIsNone(re.search(word, "\n".join(body), re.I), word)


    def body(self):
        ls, start, end = self.section()
        return "\n".join(ls[start + 1:end])

    def test_point_1_the_host_can_refuse_before_the_helper_starts(self):
        body = self.body()
        self.assertIn("before the helper starts", body)
        for part in ("the contract", "the handoff", "read access to the repository"):
            self.assertIn(part, body)

    def test_point_2_the_lead_shows_the_owner_three_things(self):
        body = self.body()
        for part in ("The lead shows the owner the host's reason",
                     "The lead shows the owner the provider that would judge",
                     "The lead shows the owner what the call would send"):
            self.assertIn(part, body)

    def test_point_3_the_judge_none_line_and_the_open_acceptance(self):
        body = self.body()
        self.assertIn("`JUDGE none`", body)
        self.assertIn("The acceptance stays open until a judge call gives a verdict", body)

    def test_point_5_no_other_way_and_no_judge_of_the_own_host(self):
        body = self.body()
        self.assertIn("The lead does not send the content another way", body)
        self.assertIn("The lead does not replace the outside judge with a judge of its own host", body)

class AmendmentTests(unittest.TestCase):
    def test_amendment_18_records_the_change(self):
        text = read("AMENDMENT-18.md")
        self.assertEqual(text.split("\n")[0], "# Amendment 18")
        for name in ("host refusal", "INSTALL.md", STYLE, PROTOCOL, SKILL):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
