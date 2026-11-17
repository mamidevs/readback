#!/usr/bin/env python3
"""Offline checks for the texts of part 1: AMENDMENT-10.md, PROTOCOL.md section 4, skills/outside-judge/SKILL.md,
output-styles/readback.md, README.md, INSTALL.md and the portable lead skill.

Run one class: python3 -B checks/test_texts_part1.py AmendmentTests
Run all:       python3 -m unittest discover -s checks -p 'test_texts_part1.py' -v

Each test reads a file as UTF-8 and asserts an exact sentence or form in the file, and in the named section where
the contract CON-20261118-05 names one. The tests show that the named sentences and forms stand in the named files.
They do not show that the prose around them is right.

No test reads a file outside the repository and no test reads the environment. AmendmentTests asserts the form of
the sentence that states the test count of the amendment, not the number.

The run stops at the first failure.
"""

import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent

# D1
STATUS_IDS = ("DEC-20261117-23", "DEC-20261117-24", "DEC-20261117-28")
STATUS_RULES = "The core rules stay Readback Protocol v0.7."
STATUS_SENTENCE = "No package ships this amendment yet; a release needs the owner's GO."

# D2: the five pieces and their files
PIECE_FILES = ("scripts/validate_handoff.py", "scripts/review_capture.py", "scripts/review_receipt.py",
               "scripts/commit_observation.py", "scripts/outside_judge.py")

# D3: the four pinned lines, as the contract states them
MUTATION_PINS = (
    ("validate_handoff.py",
     "~/logbook/readback/records/settings-design-20261117/mutation-validate_handoff-run1-lead.json",
     "5e281f1f387a7110e8918b1647336b7dbdec9bf67af42b633fc46d59c37318a8",
     "mutation result: sites 345 | killed 285 (of these by a time limit: 0) | survivors 60 | skipped 0 | baseline exits [0, 0]"),
    ("review_capture.py",
     "~/logbook/readback/records/settings-design-20261117/mutation-review_capture-run3-worker.json",
     "4da676b0584f6e1cfdb6bf4f2dbe50529ba566c9d03fd72757aedfc8814891cf",
     "mutation result: sites 121 | killed 120 | survivors 0 | skipped 1 | baseline exits [0, 0, 0]"),
    ("review_receipt.py",
     "~/logbook/readback/records/settings-design-20261117/mutation-review_receipt-run2-lead.json",
     "4677c2272bed85ac2b75c9c9b842816f6f75a0b3890df302daa5446d5068e0fc",
     "mutation result: sites 83 | killed 83 | survivors 0 | skipped 0 | baseline exits [0, 0]"),
    ("commit_observation.py",
     "~/logbook/readback/records/settings-design-20261117/mutation-commit_observation-run2-lead.json",
     "d8690885a821bd0a81173d74e8c81cb9317095228ba2291e5081b7d2da022aae",
     "mutation result: sites 203 | killed 203 (of these by a time limit: 0) | survivors 0 | skipped 0 | baseline exits [0, 0]"),
)
PART2_SENTENCE = ("Part 2 hardens the validator before its result blocks anything "
                  "(part-1-spec.md J13, in `~/logbook/readback/records/settings-design-20261117/`).")

# D4
LIMITS = (
    "The review records and the observation file are local and untrusted: the agent runs as the same user and can write them.",
    "ends-equal means that both captures succeeded with the same manifest identity; it does not establish unchanged content between captures.",
    "A stored completed record includes the structural validation result or an unavailable status; validation changes no verdict and blocks nothing in part 1.",
    "The commit observation describes; it denies nothing and decides nothing.",
    "The hook reads the command line before the Bash call starts; it does not follow cd or resolve Git aliases.",
    "The hook locates substitution boundaries without interpreting quotes or heredocs; it then tokenizes extracted text with shlex.",
    "The judge still reads the live folder.",
)

# D5
RECEIPT_FORM_1 = ("receipt    <attempt id> | <ends-equal or unbound: reason> | validation <true, false n, unparsed or unavailable> "
                  "| <path of the completed file>")
RECEIPT_FORM_2 = "receipt    none | <reason>"

# D6
THIRD_LINE = "The third line is the receipt."
NO_VERDICT = "A receipt changes no verdict."
STARTED_SENTENCE = ("When receipt setup succeeds, the helper stores a started record below the Git common directory before the "
                    "provider call; after a non-interrupted call it attempts to store a completed record.")
WITHOUT_RECORD = "Without a stored completed record, the receipt is"
FIRST_THREE = "Its first three lines identify the verdict, the execution and the receipt."
OLD_SENTENCE = "Its first two lines identify the verdict and execution."

# Corrections of the lead after its reading (CON-20261118-06 and the notes of the lead)
ONE_OF_TWO = "The helper prints one of the two receipt forms."
BY_MANIFEST = ("`<git common dir>/readback/by-manifest/` holds markers for completed records whose second capture succeeded; "
               "a failed marker write leaves the completed record without a marker.")
OBSERVE_README = ("When enabled, commit observation attempts to append one line to `<git common dir>/readback/commit-observations.jsonl` "
                  "for a command that the guard does not deny and that contains a recognized commit call or a failed-parse candidate. "
                  "The line describes the first recognized commit call in the parser's traversal order, or records a parse failure.")
OBSERVE_AMENDMENT = ("When enabled, commit observation attempts to record one line for a command that the guard does not deny and that "
                     "contains a recognized commit call or a failed-parse candidate. The line describes the first recognized commit call "
                     "in traversal order, or records a parse failure. Matching review records are listed when capture and lookup succeed.")
CAPTURE_AMENDMENT = ("A successful capture identifies the bytes and selected metadata of tracked and non-ignored untracked paths "
                     "listed by Git. It is not an atomic snapshot.")
SAFETY_NET = "The owner approved the review receipt as a safety net on 2026-11-17"
RESULT_LINE = "The line below each file is the result line of that run."

# D7
COMMIT_PATHS = ("<git common dir>/readback/reviews/", "<git common dir>/readback/by-manifest/",
                "<git common dir>/readback/commit-observations.jsonl")
CHECK_OFF = "Set READBACK_COMMIT_CHECK=off to turn the commit observation off."
NO_REVIEWS = ("Without a directory at `<git common dir>/readback/reviews`, the commit observation takes no capture and "
              "writes no observation.")
OLD_LIMIT = "The hook reads the command line only, and a hook that times out lets the command run."
NEW_LIMIT = ("The deny check examines the command line; commit observation also reads repository content and review records. "
             "A hook that times out lets the command run.")
INSTALL_NEEDS = ("Commit observation needs commit_observation.py and review_capture.py beside the portable hook; "
                 "the copied lead folder holds them.")
INSTALL_REMOVE = ("Run this from the intended repository; it deletes review records and observations shared by its main "
                  "and linked worktrees.")
INSTALL_COMMAND = 'rm -r "$(git rev-parse --git-common-dir)/readback"'


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def heading_level(line):
    match = re.match(r"(#{1,6}) ", line)
    return len(match.group(1)) if match else 0


def section(text, heading):
    """The lines of a section as text: from the heading line up to the next heading of the same or a higher level.

    A line inside a code fence never counts as a heading. The heading must stand exactly once.
    """
    lines = text.split("\n")
    level = heading_level(heading)
    assert level, heading
    starts = [number for number, line in enumerate(lines) if line == heading]
    assert len(starts) == 1, "the heading %r stands %d times" % (heading, len(starts))
    fenced = False
    taken = [lines[starts[0]]]
    for line in lines[starts[0] + 1:]:
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and 0 < heading_level(line) <= level:
            break
        taken.append(line)
    return "\n".join(taken)


def fences(text):
    """The code blocks of a text, each as a list of lines without the fence lines."""
    blocks, current = [], None
    for line in text.split("\n"):
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
        elif current is not None:
            current.append(line)
    assert current is None, "a code fence is not closed"
    return blocks


def without_fences(text):
    kept, fenced = [], False
    for line in text.split("\n"):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced:
            kept.append(line)
    return "\n".join(kept)


class TextCase(unittest.TestCase):
    def assert_receipt_block(self, text, where):
        """One code block of the text holds JUDGE, execution, form 1 and form 2 as whole lines, in this order."""
        found = []
        for block in fences(text):
            if RECEIPT_FORM_1 in block:
                found.append(block)
        self.assertEqual(len(found), 1, where + ": the first receipt form stands in %d code blocks" % len(found))
        block = found[0]
        self.assertTrue(block[0].startswith("JUDGE      "), where + ": the block starts with the JUDGE line")
        self.assertTrue(block[1].startswith("execution  "), where + ": the second line is the execution line")
        self.assertEqual(block[2:], [RECEIPT_FORM_1, RECEIPT_FORM_2], where + ": the receipt forms follow, whole lines")
        self.assertEqual(text.count(RECEIPT_FORM_2 + "\n```\n\n" + ONE_OF_TWO + "\n"), 1,
                         where + ": the sentence on the two forms stands directly after the code block")

    def assert_once(self, text, sentence, where):
        self.assertEqual(text.count(sentence), 1, "%s holds %r %d times" % (where, sentence, text.count(sentence)))


class AmendmentTests(TextCase):
    def setUp(self):
        self.text = read("AMENDMENT-10.md")

    def test_status_paragraph(self):
        paragraphs = self.text.split("\n\n")
        self.assertEqual(paragraphs[0], "# Amendment 10")
        status = paragraphs[1]
        self.assertTrue(status.startswith("Status: "))
        for decision in STATUS_IDS:
            self.assertIn(decision, status)
        self.assertIn(STATUS_RULES, status)
        self.assertIn(STATUS_SENTENCE, status)
        self.assertIn(SAFETY_NET + ': "onayliyorum" (DEC-20261117-24).', self.text.split("\n\n")[2])

    def test_table_has_one_row_for_each_piece(self):
        rows = [line for line in self.text.split("\n") if line.startswith("|")]
        self.assertEqual(rows[0], "| Piece | Gap | Change |")
        self.assertEqual(rows[1], "|---|---|---|")
        body = rows[2:]
        self.assertEqual(len(body), len(PIECE_FILES))
        for name in PIECE_FILES:
            holding = [row for row in body if "`" + name + "`" in row]
            self.assertEqual(len(holding), 1, "the table has %d rows that name %s" % (len(holding), name))
            self.assertEqual(holding[0].count("|"), 4, name + ": the row has three cells")
        for name in PIECE_FILES:
            self.assertEqual(sum(1 for row in body if name in row), 1, name + " is named in one row only")
        capture = [row for row in body if "`scripts/review_capture.py`" in row][0]
        observation = [row for row in body if "`scripts/commit_observation.py`" in row][0]
        self.assertTrue(capture.endswith(" | " + CAPTURE_AMENDMENT + " |"))
        self.assertTrue(observation.endswith(" | " + OBSERVE_AMENDMENT + " |"))

    def test_measurements_lines(self):
        measurements = section(self.text, "## Measurements")
        lines = measurements.split("\n")
        self.assertEqual(measurements.count(RESULT_LINE), 1)
        found = [line for line in lines if line.startswith("mutation result:")]
        self.assertEqual(found, [pin[3] for pin in MUTATION_PINS])
        for name, path, digest, line in MUTATION_PINS:
            self.assertEqual(
                lines.count("`%s`: file `%s`, sha256 `%s`." % (name, path, digest)), 1, name + ": file and sha256 line")
            # the pinned line stands alone in a code block
            self.assertIn([line], fences(measurements), name + ": the line stands alone in a code block")

    def test_measurements_validator_survivors_and_part2(self):
        measurements = section(self.text, "## Measurements")
        self.assertIn("survivors 60", measurements)
        self.assertIn("The validator tests leave 60 of 345 mutants alive. In part 1 the validation result is recorded and blocks "
                      "nothing. " + PART2_SENTENCE, measurements)

    def test_count_sentence_form(self):
        measurements = section(self.text, "## Measurements")
        form = re.compile(r"The offline suite ran (\d+) tests at the last change of this amendment; "
                          r"all passed and none was skipped\.")
        lines = [line for line in measurements.split("\n") if form.fullmatch(line)]
        self.assertEqual(len(lines), 1, "the Measurements section holds one count sentence on its own line")
        self.assertGreater(int(form.fullmatch(lines[0]).group(1)), 0)

    def test_limits(self):
        limits = section(self.text, "## Limits")
        lines = limits.split("\n")
        self.assertEqual(lines[0], "## Limits")
        for sentence in LIMITS:
            self.assertEqual(lines.count("- " + sentence), 1, sentence)
        self.assertLess(self.text.index("## Measurements"), self.text.index("## Limits"))


class ReceiptTests(TextCase):
    def test_protocol_section_4(self):
        text = section(read("PROTOCOL.md"), "## 4. Independence")
        self.assert_receipt_block(text, "PROTOCOL.md section 4")
        prose = without_fences(text)
        lines = prose.split("\n")
        self.assert_once(prose, THIRD_LINE, "PROTOCOL.md section 4")
        self.assert_once(prose, NO_VERDICT, "PROTOCOL.md section 4")
        self.assertEqual(lines.count(STARTED_SENTENCE), 1)
        self.assert_once(prose, WITHOUT_RECORD + " `" + RECEIPT_FORM_2 + "`.", "PROTOCOL.md section 4")
        self.assertEqual(lines.count(LIMITS[0]), 1)
        self.assertEqual(lines.count(LIMITS[1]), 1)

    def test_outside_judge_skill(self):
        text = read("skills/outside-judge/SKILL.md")
        self.assert_receipt_block(text, "skills/outside-judge/SKILL.md")
        prose = without_fences(text)
        self.assert_once(prose, FIRST_THREE, "skills/outside-judge/SKILL.md")
        self.assert_once(prose, THIRD_LINE, "skills/outside-judge/SKILL.md")
        self.assert_once(prose, NO_VERDICT, "skills/outside-judge/SKILL.md")
        self.assertIn("3. Read the complete output. " + FIRST_THREE + "\n", text)

    def test_output_style_accept(self):
        text = section(read("output-styles/readback.md"), "## ACCEPT: after a worker stops")
        self.assert_receipt_block(text, "output-styles/readback.md ACCEPT")
        prose = without_fences(text)
        self.assert_once(prose, FIRST_THREE, "output-styles/readback.md ACCEPT")
        self.assert_once(prose, THIRD_LINE, "output-styles/readback.md ACCEPT")
        self.assert_once(prose, NO_VERDICT, "output-styles/readback.md ACCEPT")

    def test_the_old_sentence_is_absent(self):
        for relative in ("skills/outside-judge/SKILL.md", "PROTOCOL.md", "output-styles/readback.md",
                         "portable/skills/readback/SKILL.md"):
            self.assertNotIn(OLD_SENTENCE, read(relative), relative)


class ReadmeTests(TextCase):
    def test_readme_judge_section(self):
        text = section(read("README.md"), "## A judge at the other provider")
        for path in COMMIT_PATHS:
            self.assertIn("`" + path + "`", text)
        self.assert_once(text, CHECK_OFF, "README.md judge section")
        self.assert_once(text, NO_REVIEWS, "README.md judge section")
        self.assert_once(text, LIMITS[3], "README.md judge section")
        self.assert_once(text, BY_MANIFEST, "README.md judge section")
        self.assert_once(text, OBSERVE_README, "README.md judge section")

    def test_readme_checks_and_limits(self):
        text = read("README.md")
        limits = section(text, "## Checks and limits")
        self.assert_once(limits, NEW_LIMIT, "README.md Checks and limits")
        self.assertNotIn(OLD_LIMIT, text)

    def test_readme_source_map(self):
        rows = [line for line in section(read("README.md"), "## Source map").split("\n") if line.startswith("| ")]
        for name in PIECE_FILES:
            holding = [row for row in rows if row.startswith("| [%s](%s) |" % (name, name))]
            self.assertEqual(len(holding), 1, "the source map has %d rows for %s" % (len(holding), name))

    def test_install_git_guard(self):
        text = section(read("INSTALL.md"), "### Git guard for Codex")
        lines = text.split("\n")
        self.assertEqual(lines.count(INSTALL_NEEDS), 1)
        self.assertEqual(lines.count(CHECK_OFF), 1)

    def test_install_remove(self):
        text = section(read("INSTALL.md"), "## Remove")
        lines = text.split("\n")
        self.assertEqual(lines.count(INSTALL_REMOVE), 1)
        index = lines.index(INSTALL_REMOVE)
        self.assertEqual(lines[index + 1:index + 4], ["```", INSTALL_COMMAND, "```"])


class SyncTests(TextCase):
    def test_sync_check_exits_zero(self):
        done = subprocess.run([sys.executable, "-B", "scripts/sync.py", "--check"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "portable skills are current"), done.stdout + done.stderr)

    def test_lead_changes_stand_once_in_the_style(self):
        spec = importlib.util.spec_from_file_location("readback_sync", ROOT / "scripts" / "sync.py")
        module = importlib.util.module_from_spec(spec)
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
        style = read("output-styles/readback.md")
        for old, _new in module.LEAD_CHANGES:
            self.assertEqual(style.count(old), 1, old[:60])

    def test_generated_lead_skill_holds_both_forms(self):
        text = read("portable/skills/readback/SKILL.md")
        self.assert_receipt_block(text, "portable/skills/readback/SKILL.md")
        prose = without_fences(text)
        self.assert_once(prose, FIRST_THREE, "portable/skills/readback/SKILL.md")
        self.assert_once(prose, THIRD_LINE, "portable/skills/readback/SKILL.md")
        self.assert_once(prose, NO_VERDICT, "portable/skills/readback/SKILL.md")
        self.assertNotIn(OLD_SENTENCE, text)


if __name__ == "__main__":
    unittest.main(failfast=True)
