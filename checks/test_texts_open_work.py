#!/usr/bin/env python3
"""Offline checks for the open-work texts of CON-20261123-09 (ACC-5).

Run: python3 -m unittest discover -s checks -p 'test_texts_open_work.py' -v
"""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent

S1 = "Take the open contracts and handoffs from `scripts/open_work.py`."
S2 = "Name its conflicts and its unlinked count with them."
ITEM = "- " + S1 + " " + S2
BEFORE = "- After each BRIEF, publish OPEN and NEXT"

STYLE = ("output-styles/readback.md", "CURRENT: after each brief")
PROTOCOL = ("PROTOCOL.md", "6. Resume")
PORTABLE = ("portable/skills/readback/SKILL.md", "CURRENT: after each brief")


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def section(text, heading):
    match = re.search(r"^#+ " + re.escape(heading) + r"[^\n]*\n(.*?)(?=^#+ |\Z)", text, re.S | re.M)
    assert match, heading
    return match.group(1)


def lines_of(rel, heading):
    return section(read(rel), heading).split("\n")


class SentenceTests(unittest.TestCase):
    def test_the_item_stands_once_in_its_section_of_the_style_and_of_protocol(self):
        for where in (STYLE, PROTOCOL):
            with self.subTest(where):
                self.assertEqual(lines_of(*where).count(ITEM), 1)

    def test_the_item_stands_directly_after_the_after_each_brief_item(self):
        for where in (STYLE, PROTOCOL):
            with self.subTest(where):
                lines = lines_of(*where)
                before = [i for i, l in enumerate(lines) if l.startswith(BEFORE)]
                self.assertEqual(len(before), 1)
                self.assertEqual(lines[before[0] + 1], ITEM)

    def test_the_sentence_stands_nowhere_else_in_the_style_and_protocol(self):
        for rel in ("output-styles/readback.md", "PROTOCOL.md"):
            with self.subTest(rel):
                self.assertEqual(read(rel).count(S1), 1)
                self.assertEqual(read(rel).count(S2), 1)

    def test_each_sentence_stays_inside_the_word_limit(self):
        for sentence in (S1, S2):
            self.assertLessEqual(len(sentence.split()), 20, sentence)

    def test_the_neighbouring_items_stay(self):
        for where in (STYLE, PROTOCOL):
            with self.subTest(where):
                lines = lines_of(*where)
                self.assertIn("- After each BRIEF, publish OPEN and NEXT for the same repository, task and worktree using that snapshot. "
                              "Keep GOAL and IN FORCE shared.", lines)
                self.assertTrue(any(l.startswith("- If a task update conflicts, reread that task") for l in lines))


class PortableTests(unittest.TestCase):
    def test_the_portable_lead_skill_holds_the_item_after_the_brief_item(self):
        lines = lines_of(*PORTABLE)
        self.assertEqual(lines.count(ITEM), 1)
        before = [i for i, l in enumerate(lines) if l.startswith(BEFORE)]
        self.assertEqual(len(before), 1)
        self.assertEqual(lines[before[0] + 1], ITEM)

    def test_the_portable_lead_skill_holds_the_script(self):
        self.assertEqual(read("portable/skills/readback/scripts/open_work.py"), read("scripts/open_work.py"))

    def test_the_portable_copy_of_the_changed_closure_script_is_current(self):
        self.assertEqual(read("portable/skills/readback/scripts/closure_note.py"), read("scripts/closure_note.py"))


class SyncTests(unittest.TestCase):
    def test_sync_lists_the_script_at_the_end_of_the_helper_tuple_after_closure_note(self):
        text = read("scripts/sync.py")
        self.assertIn('"handoff_skeleton.py", "closure_note.py", "open_work.py"):', text)
        self.assertEqual(text.count('"open_work.py"'), 1)

    def test_sync_keeps_the_base_helper_names_in_base_order_once_and_ends_with_open_work(self):
        text = read("scripts/sync.py")
        tuple_text = text[text.index("for helper in ("):text.index("):", text.index("for helper in ("))]
        names = re.findall(r'"([a-z_]+\.py)"', tuple_text)
        base = ["project_registry.py", "resolve_codex_project.py", "resolve_claude_project.py", "project_index.py",
                "publish_records.py", "task_current.py", "review_receipt.py", "review_capture.py", "review_redaction.py",
                "review_retention.py", "validate_handoff.py", "commit_observation.py", "readback_settings.py",
                "readback_session.py", "judge_rounds.py", "handoff_skeleton.py", "closure_note.py"]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[-1], "open_work.py")
        rest = iter(names)
        for name in base:
            self.assertIn(name, rest, "base name missing or out of base order: " + name)


class AmendmentTests(unittest.TestCase):
    def test_amendment_15_records_the_change(self):
        text = read("AMENDMENT-15.md")
        for needle in ("# Amendment 15", "CON-20261123-09", "No package ships this amendment yet",
                       "scripts/open_work.py", "scripts/closure_note.py withdrawal", "scripts/sync.py",
                       "checks/test_open_work.py", "checks/test_withdrawal_note.py", "checks/test_texts_open_work.py"):
            with self.subTest(needle):
                self.assertIn(needle, text)

    def test_amendment_15_holds_the_measured_reasons(self):
        text = read("AMENDMENT-15.md")
        for needle in ("on 2026-11-23 the lead rewrote the open work of one task block 15 times by hand",
                       "CON-20261123-05 replaced CON-20261123-04 and the only trace is prose in a LOG line",
                       "in another project six free-form notes hold verdicts that a first count read as unjudged handoffs",
                       "one of them names two handoffs that were never sent to the judge",
                       "of the 135 handoffs of this project that were stored at the time of the count, none holds `contract_id`, "
                       "62 name no contract id in their text and 21 name more than one"):
            with self.subTest(needle):
                self.assertIn(needle, text)

    def test_amendment_15_holds_the_limits(self):
        text = read("AMENDMENT-15.md").lower()
        for needle in ("the script reads form and links; it does not show that a verdict is right",
                       "a contract without a linked handoff is `unbound`, not closed"):
            with self.subTest(needle):
                self.assertIn(needle, text)

    def test_amendment_15_names_the_conflicts_and_the_states(self):
        text = read("AMENDMENT-15.md")
        for needle in ("accepted-and-replaced", "missing-target", "contract-mismatch", "withdrawn-and-closed",
                       "invalid-note", "unreadable", "`conflict`", "`rejected`", "`unbound`"):
            with self.subTest(needle):
                self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
