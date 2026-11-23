#!/usr/bin/env python3
"""Offline checks for the dispatch texts of CON-20261123-01.

Run: python3 -m unittest discover -s checks -p 'test_texts_dispatch.py' -v
"""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent

SENTENCES = (
    "Put the complete contract in the worker's prompt, or the path of its record.",
    "The complete contract holds its statements, their checks and the out-of-scope line.",
    "A contract id alone delivers no statement.",
)
UNABLE_LINE = ("If the task names a contract and you cannot read its statements, return UNABLE. "
               "Name the contract. Change no file.")
NO_CONTRACT_LINE = "Without a contract, write `done when:` in plain words and `contract: none`."


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def section(text, heading):
    match = re.search(r"^#+ " + re.escape(heading) + r"[^\n]*\n(.*?)(?=^#+ |\Z)", text, re.S | re.M)
    assert match, heading
    return match.group(1)


class ContractTextTests(unittest.TestCase):
    def check_file(self, rel, heading):
        body = section(read(rel), heading)
        lines = body.split("\n")
        positions = [lines.index(s) for s in SENTENCES]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(set(positions)), 3)

    def test_output_style_contract_section(self):
        self.check_file("output-styles/readback.md", "CONTRACT")

    def test_protocol_section_3_2(self):
        self.check_file("PROTOCOL.md", "3.2 CONTRACT")


class WorkerTextTests(unittest.TestCase):
    def test_before_you_start(self):
        body = section(read("agents/worker.md"), "Before you start")
        lines = body.split("\n")
        self.assertIn(UNABLE_LINE, lines)
        self.assertIn(NO_CONTRACT_LINE, lines)

    def test_portable_skill_holds_texts(self):
        self.assertIn(UNABLE_LINE, read("portable/skills/readback-worker/SKILL.md"))
        for sentence in SENTENCES:
            self.assertIn(sentence, read("portable/skills/readback/SKILL.md"))


class InstallAmendmentTests(unittest.TestCase):
    def test_install_says_claude_only(self):
        text = read("INSTALL.md")
        self.assertIn("The hook runs in Claude Code only.", text)
        self.assertIn("A Codex lead has the two text rules only", text)

    def test_amendment_12(self):
        text = read("AMENDMENT-12.md")
        self.assertIn("DEC-20261123-07", text)
        self.assertIn("No package ships this amendment yet", text)
        self.assertIn("hooks/guard_dispatch.py", text)


if __name__ == "__main__":
    unittest.main()
