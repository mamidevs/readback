#!/usr/bin/env python3
"""Offline checks for the texts of part 2: AMENDMENT-11.md, PROTOCOL.md, output-styles/readback.md,
skills/outside-judge/SKILL.md, the agent descriptions, README.md, INSTALL.md and the portable lead skill.

Run one class: python3 -B checks/test_texts_part2.py AmendmentTests
Run all:       python3 -m unittest discover -s checks -p 'test_texts_part2.py' -v

Each test reads a file as UTF-8 and asserts an exact sentence or form in the file, and in the named section
where the contract CON-20261118-19 names one. The tests show that the named sentences and forms stand in the
named files. They do not show that the prose around them is right.

No test reads a file outside the repository and no test reads the environment. MeasurementTests holds the
result lines of the lead's mutation runs as constants; it does not read the result files.

The run stops at the first failure.
"""

import hashlib
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent

# D1
STATUS_IDS = ("DEC-20261117-23", "DEC-20261117-28", "DEC-20261118-01")
STATUS_RULES = "The rules are Readback Protocol v0.8 from this amendment on."
STATUS_SENTENCE = "No package ships this amendment yet; a release needs the owner's GO."

# D2: the pieces, each with its files
PIECES = (
    ("Settings", ("scripts/readback_settings.py", "schemas/settings.schema.json")),
    ("Session state", ("scripts/readback_session.py",)),
    ("Structural validator 2.0.0", ("scripts/validate_handoff.py",)),
    ("Judge line", ("hooks/judge_line.py",)),
    ("Commit observation switch", ("hooks/guard_git.py",)),
    ("Helper modes", ("scripts/outside_judge.py",)),
    ("Round accounting", ("scripts/judge_rounds.py",)),
    ("Legacy home of a linked worktree", ("scripts/project_registry.py",)),
)
TABLE_HEADER = "| Piece | Gap | Change |"

# D3
MODE_HEADER = "| Mode | Step handoff | Gate handoff |"
MODE_ROWS = (
    "| off | no judge; JUDGE off | no judge; JUDGE off |",
    "| gate | no judge; JUDGE skipped | outside judge |",
    "| each | outside judge; a step PASS accepts nothing | outside judge |",
)
MODE_DEFAULT = "The default mode is gate."

# D4
OWNER_FORMS = ("`JUDGE off|gate|each [session]`", "`JUDGE default`", "`JUDGE round <CON id> <step|gate>`")
OWNER_SENTENCES = (
    "The owner writes a judge line as the first line of a prompt.",
    "A stored mode override lasts until another judge line replaces it or a cleanup removes it; JUDGE default clears it.",
    "A stored round authorization permits one matching reservation.",
    "A successful cleanup at SessionStart with the source resume or fork clears the override and the unused authorizations.",
    "An agent never writes a judge line and never asks a tool to write one.",
)

# D6
LIMITS = (
    "Every protection of this amendment is a safety net: the agent runs as the same user, can write the files, edit the hooks and call a provider directly.",
    "Local records do not authenticate who wrote them.",
    "JUDGE off and JUDGE skipped end with exit status 0; read the word after JUDGE, never the exit status alone.",
    "A record of off or skipped binds no content and gives no eligibility for a commit.",
    "The commit check observes and never blocks in this part.",
    "Sampled Claude hand-backs begin with framing; the event fields do not authenticate the owner as the author.",
    "Under Codex the hook behaviour and the session variable are documented, not measured.",
    "A settings file that exists hides the legacy table; migrate-teammates shows the mapping, and --write fills only the settings leaves that are missing.",
    "Within one project, repository, lineage and tier, the FAIL verdicts and the pending attempts fill judge.max_fail_rounds slots, 3 by default.",
    "A new lineage starts a new count.",
    "The helper needs the git command; without it the project cannot be resolved and the helper gives no verdict.",
)

# D7: PROTOCOL.md and output-styles/readback.md
ACCEPT_SENTENCES = (
    "Accept only a DONE handoff whose contract statements are all VERIFIED.",
    "The worker may start other assigned, independent work as soon as it submits the handoff.",
    "Use one trusted run of the contract's named checks for the submitted commit.",
    "Accept a routine step when structural validation is true, the join is conflict-free and the integration suite passes on the joined revision.",
    "Escalate a failed or missing check, ambiguous scope or evidence, a denied action, a conflict or a high-risk change to the lead.",
    "Name the part's checks in its contract before work starts. Each check must exercise the behaviour the owner requested.",
    "After all pieces join, run those checks on the clean joined revision. Keep the revision, commands, outputs and results together.",
    "Close the part only when those checks pass and cover every requested outcome. Record any remaining outcome as open.",
    "In mode each, the outside judge screens each step; a step PASS accepts nothing, and a step FAIL sends the work back.",
    "An acceptance without a judge needs structural validation true.",
    "In the modes gate and each, the acceptance of a part needs a gate PASS.",
    "The owner's approval of an action stays separate.",
    "JUDGE off and JUDGE skipped are not PASS.",
    "Copy the helper's complete JUDGE line; ACCEPTED records what the acceptance rests on.",
    "The lead passes --coverage for a gate call and lists the worker contracts and the original handoffs.",
    "The helper allows a gate call without a list and checks neither the completeness of the list nor the judge's access to the files.",
    "At the limit of the rounds another reservation needs a matching authorization of the owner.",
    "A changed digest of the effective settings needs a matching authorization; one authorization covers both exceptions of one reservation.",
)
OLD_PROTOCOL_RULE = "The lead accepts the work when the state is DONE and the judge says PASS."
OLD_STYLE_RULE = "Accept the work when the state is DONE and the judge says PASS."

# D7: skills/outside-judge/SKILL.md
SKILL_SENTENCES = (
    "Exit code 0 with JUDGE off or JUDGE skipped is not a verdict.",
    "After the checks of the arguments, the settings, the session and the inputs, structural validation runs before the mode decides; "
    "false or unavailable blocks a provider call, and unparsed goes to the judge with its findings.",
    "Pass --tier step for one piece and --tier gate for the handoff of a part.",
    "Do not change the mode, a route or a deadline to get another answer.",
)

# D7: README.md
README_MODES = "The judge runs in one of three modes: off, gate and each."
README_SHOW = "python3 scripts/readback_settings.py --host <host> show"
README_SET = "set <key> <value>"
README_MIGRATE = "migrate-teammates"
README_FILE = "The settings file is <project home>/settings.json."

# D7: INSTALL.md
INSTALL_EVENTS = ("UserPromptSubmit", "SessionStart", "SessionEnd")
INSTALL_CODEX = ("Merge the judge-line entries into $CODEX_HOME/hooks.json (default ~/.codex/hooks.json), trust them with /hooks, "
                 "register each entry once and check one live prompt; without these steps a judge line changes nothing under Codex.")
INSTALL_FOLDERS = ("~/logbook/.readback/sessions/", "~/logbook/.readback/rounds/")

# D8
VERSION_SENTENCE = ("This package keeps protocol_version 0.7 in project profiles and in the registry; part 4 of the redesign owns "
                    "the compatibility in scripts/project_registry.py, the profile, project and registry schemas, and the tests "
                    "that keep 0.7 projects readable without a silent rewrite.")

CURRENT_VERSION_SENTENCE = ("Profiles, project identities and registry entries support protocol versions 0.7 and 0.8. "
                            "Reading an existing project preserves its recorded version.")

# D9
COMMAND_FORM = ("outside-judge.sh --host <host> [--tier step|gate] [--lineage <CON id>] [--coverage <file>] "
                "<contract file> <handoff file> <repository folder>")
COMMAND_OPTIONS = ("--tier", "--lineage", "--coverage")
FIRST_LINES = (
    "JUDGE      PASS | <provider:model> | <repo@sha or none> | <note>",
    "JUDGE      FAIL | <provider:model> | <repo@sha or none> | <note>",
    "JUDGE      none | <provider:model> | <repo@sha or none> | no verdict: <reason>",
    "JUDGE      off | none:none | <repo@sha or none> | judge mode off (<source>); no provider call<tail>",
    "JUDGE      skipped | none:none | <repo@sha or none> | judge mode gate skips a step review (<source>); no provider call<tail>",
)
TAIL_SENTENCE = ("`<tail>` is empty for validation true; otherwise it is "
                 "`; checks-only acceptance is not established: <validation phrase>`.")
OFF_EXECUTION = "execution  none | none | none | cli | none"
OFF_RECEIPT_1 = "receipt    <attempt id> | no review | <validation phrase> | <path>"
OFF_RECEIPT_2 = "receipt    none | <reason>"

# AMENDMENT-08.md, AMENDMENT-09.md and AMENDMENT-10.md as they stood at the base 24c7146577ec8ab13ebf26317041ee9d11ce0092
OLD_AMENDMENTS = (
    ("AMENDMENT-08.md", "c198228bf74b286822cad7a4518d6c97585501535a1fe4bb0862ff03640082db"),
    ("AMENDMENT-09.md", "87e3628080b46fb69ef1a054149a53e9e0614d9771a70f9c16f083070d08b1e5"),
    ("AMENDMENT-10.md", "183811a02cd55ecbc15ae3cb3a413adf8d3a4e38a27f796677a47ac2344eda98"),
)


MEASUREMENT_COMMIT = "0323483179f1d08493c5edbca941db8f8b6ba15b"
MEASUREMENT_FOLDER = "`~/logbook/readback/records/settings-design-20261117/part-2-measurements-0323483/`"
MEASUREMENTS = (
    ("scripts/readback_settings.py", "settings.json", "8c7ad97a220b64d752de722e713426ca2719be2532eaa46bee9a7199175ed81e",
     "mutation result: sites 166 | killed 166 (of these by a time limit: 0) | survivors 0 | skipped 0 | baseline exits [0, 0]"),
    ("scripts/readback_session.py", "session.json", "a3a7ecc85f0f8ebdc1fa632769645deee94cce6f437df27084f029d92a0f9b1b",
     "mutation result: sites 140 | killed 140 (of these by a time limit: 0) | survivors 0 | skipped 0 | baseline exits [0, 0]"),
    ("scripts/validate_handoff.py", "validator.json", "8227a1cd37093582cab65ef93d7f334fa76f0fc700ce723460a34171e9e26079",
     "mutation result: sites 390 | killed 390 (of these by a time limit: 0) | survivors 0 | skipped 0 | baseline exits [0, 0]"),
    ("hooks/judge_line.py", "line.json", "92d5caf0e37f600a7a3d970a99c76bf7d57a4b12d87fbf540c2fc4a850be9abb",
     "mutation result: sites 77 | killed 72 (of these by a time limit: 0) | survivors 5 | skipped 0 | baseline exits [0, 0]"),
    ("scripts/judge_rounds.py", "roundsall.json", "d4fb757d1482d23070f8736ce370691870cad3052e9239c4bb6a88ccbf94bdea",
     "mutation result: sites 261 | killed 257 (of these by a time limit: 0) | survivors 4 | skipped 0 | baseline exits [0, 0]"),
)
MEASUREMENT_SURVIVORS = (
    "The five survivors of `hooks/judge_line.py` (lines 31, 109, 113, 122 and 126) are the equivalent mutants that the lead ruled on in HND-20261118-16.",
    "The four survivors of `scripts/judge_rounds.py` are equivalent:",
)


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
    def assert_once(self, text, sentence, where):
        self.assertEqual(text.count(sentence), 1, "%s holds %r %d times" % (where, sentence[:70], text.count(sentence)))

    def assert_sentences(self, text, sentences, where):
        for sentence in sentences:
            self.assert_once(text, sentence, where)

    def assert_mode_table(self, text, where):
        """The header, the separator, the three rows, an empty line and the default sentence stand together, once."""
        lines = text.split("\n")
        self.assertEqual(lines.count(MODE_HEADER), 1, where + ": the mode table header stands once")
        start = lines.index(MODE_HEADER)
        self.assertTrue(lines[start + 1].startswith("|---"), where + ": the separator follows the header")
        self.assertEqual(tuple(lines[start + 2:start + 5]), MODE_ROWS, where + ": the three rows follow, in order")
        self.assertEqual(lines[start + 5:start + 7], ["", MODE_DEFAULT], where + ": the default sentence follows the table")
        self.assertEqual(text.count(MODE_DEFAULT), 1, where + ": the default sentence stands once")

    def assert_first_lines_block(self, text, where):
        """One code block of the text holds the five first-line forms as whole lines, in this order."""
        found = [block for block in fences(text) if FIRST_LINES[0] in block]
        self.assertEqual(len(found), 1, where + ": the PASS form stands in %d code blocks" % len(found))
        self.assertEqual(found[0], list(FIRST_LINES), where + ": the block holds the five forms and nothing else")

    def assert_version_sentence(self, text, where):
        self.assert_once(text, VERSION_SENTENCE, where)


class AmendmentTests(TextCase):
    def setUp(self):
        self.text = read("AMENDMENT-11.md")

    def test_status_line(self):
        paragraphs = self.text.split("\n\n")
        self.assertEqual(paragraphs[0], "# Amendment 11")
        status = paragraphs[1]
        self.assertTrue(status.startswith("Status: "))
        for decision in STATUS_IDS:
            self.assertIn(decision, status)
        self.assertIn(STATUS_RULES, status)
        self.assertIn(STATUS_SENTENCE, status)

    def test_table_has_one_row_for_each_piece(self):
        rows = [line for line in self.text.split("\n") if line.startswith("|")]
        mode_rows = [row for row in rows if row == MODE_HEADER or row in MODE_ROWS]
        rows = [row for row in rows if row not in mode_rows and not row.startswith("|---")]
        self.assertEqual(rows[0], TABLE_HEADER)
        body = rows[1:]
        self.assertEqual(len(body), len(PIECES))
        for label, files in PIECES:
            holding = [row for row in body if row.startswith("| " + label + ", `")]
            self.assertEqual(len(holding), 1, "the table has %d rows for %s" % (len(holding), label))
            self.assertEqual(holding[0].count("|"), 4, label + ": the row has three cells")
            for name in files:
                self.assertIn("`" + name + "`", holding[0], label + " names " + name)
                self.assertEqual(sum(1 for row in body if name in row), 1, name + " is named in one row only")

    def test_mode_table(self):
        self.assert_mode_table(self.text, "AMENDMENT-11.md")

    def test_owner_channel(self):
        channel = section(self.text, "## Owner channel")
        for form in OWNER_FORMS:
            self.assert_once(channel, form, "## Owner channel")
        self.assert_sentences(channel, OWNER_SENTENCES, "## Owner channel")

    def test_limits(self):
        limits = section(self.text, "## Limits")
        lines = limits.split("\n")
        self.assertEqual(lines[0], "## Limits")
        for sentence in LIMITS:
            self.assertEqual(lines.count("- " + sentence), 1, sentence)

    def test_version_sentence(self):
        self.assert_version_sentence(self.text, "AMENDMENT-11.md")


class ProtocolTests(TextCase):
    def test_protocol_names_v08(self):
        lines = read("PROTOCOL.md").split("\n")
        self.assertEqual(lines[0], "# Readback Protocol v0.8")
        self.assertTrue(lines[2].startswith("Status: approved. This text is Readback Protocol v0.8."), lines[2])

    def test_output_style_names_v08(self):
        lines = read("output-styles/readback.md").split("\n")
        self.assertEqual(lines[1], "name: Readback")
        self.assertTrue(lines[2].startswith("description: Readback Protocol v0.8 "), lines[2])
        self.assertEqual(lines[6], "You work for one owner. Follow Readback Protocol v0.8 when you speak to the owner.")

    def test_agent_descriptions_name_v08(self):
        for role in ("explorer", "judge", "worker"):
            description = [line for line in read("agents/%s.md" % role).split("\n") if line.startswith("description: ")]
            self.assertEqual(len(description), 1, role)
            self.assertIn("Readback Protocol v0.8", description[0], role)
            self.assertNotIn("v0.7", description[0], role)

    def test_mode_table_in_protocol(self):
        self.assert_mode_table(section(read("PROTOCOL.md"), "## 4. Independence"), "PROTOCOL.md section 4")

    def test_mode_table_in_output_style(self):
        self.assert_mode_table(section(read("output-styles/readback.md"), "## ACCEPT: after a worker stops"),
                               "output-styles/readback.md ACCEPT")

    def test_accept_sentences_in_protocol(self):
        text = section(read("PROTOCOL.md"), "## 4. Independence")
        self.assert_sentences(text, ACCEPT_SENTENCES, "PROTOCOL.md section 4")
        self.assertNotIn(OLD_PROTOCOL_RULE, read("PROTOCOL.md"))

    def test_accept_sentences_in_output_style(self):
        text = section(read("output-styles/readback.md"), "## ACCEPT: after a worker stops")
        self.assert_sentences(text, ACCEPT_SENTENCES, "output-styles/readback.md ACCEPT")
        self.assertNotIn(OLD_STYLE_RULE, read("output-styles/readback.md"))

    def test_version_sentence_in_protocol(self):
        self.assert_once(read("PROTOCOL.md"), CURRENT_VERSION_SENTENCE, "PROTOCOL.md")

    def test_version_sentence_in_output_style(self):
        self.assert_once(read("output-styles/readback.md"), CURRENT_VERSION_SENTENCE, "output-styles/readback.md")

    def test_old_amendments_are_unchanged(self):
        for name, digest in OLD_AMENDMENTS:
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest, name)


class SkillTests(TextCase):
    def setUp(self):
        self.text = read("skills/outside-judge/SKILL.md")

    def test_command_form_with_three_options(self):
        blocks = [block for block in fences(self.text) if COMMAND_FORM in block]
        self.assertEqual(len(blocks), 1, "the command form stands in %d code blocks" % len(blocks))
        self.assertIn(COMMAND_FORM, blocks[0])
        for option in COMMAND_OPTIONS:
            self.assertIn(option + " ", COMMAND_FORM)
        self.assertEqual(self.text.count(COMMAND_FORM), 1)

    def test_five_first_line_forms(self):
        self.assert_first_lines_block(self.text, "skills/outside-judge/SKILL.md")

    def test_tail_and_the_lines_of_off_and_skipped(self):
        self.assert_once(without_fences(self.text), TAIL_SENTENCE, "skills/outside-judge/SKILL.md")
        found = [block for block in fences(self.text) if OFF_EXECUTION in block]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0], [OFF_EXECUTION, OFF_RECEIPT_1, OFF_RECEIPT_2])

    def test_rules(self):
        self.assert_sentences(without_fences(self.text), SKILL_SENTENCES, "skills/outside-judge/SKILL.md")


class ReadmeTests(TextCase):
    def test_readme_modes_and_settings_command(self):
        text = section(read("README.md"), "## A judge at the other provider")
        self.assert_once(text, README_MODES, "README.md judge section")
        self.assert_once(text, README_FILE, "README.md judge section")
        blocks = [block for block in fences(text) if README_SHOW in block]
        self.assertEqual(len(blocks), 1, "the show command stands in %d code blocks" % len(blocks))
        joined = "\n".join(blocks[0])
        self.assertIn(README_SET, joined)
        self.assertIn(README_MIGRATE, joined)
        self.assertEqual(blocks[0][0], README_SHOW)

    def test_readme_source_map(self):
        rows = [line for line in section(read("README.md"), "## Source map").split("\n") if line.startswith("| ")]
        names = ["scripts/readback_settings.py", "scripts/readback_session.py", "scripts/validate_handoff.py",
                 "hooks/judge_line.py", "hooks/guard_git.py", "scripts/outside_judge.py", "scripts/judge_rounds.py",
                 "scripts/project_registry.py"]
        for _label, files in PIECES:
            for name in files:
                if name.endswith(".py"):
                    self.assertIn(name, names)
        for name in names:
            holding = [row for row in rows if row.startswith("| [%s](%s) |" % (name, name))]
            self.assertEqual(len(holding), 1, "the source map has %d rows for %s" % (len(holding), name))

    def test_install_events_for_both_hosts(self):
        text = read("INSTALL.md")
        claude = section(text, "### Judge line for Claude Code")
        codex = section(text, "### Judge line for Codex")
        for event in INSTALL_EVENTS:
            self.assertIn(event, claude, "Claude Code: " + event)
            self.assertIn(event, codex, "Codex: " + event)

    def test_install_codex_sentence(self):
        self.assert_once(section(read("INSTALL.md"), "### Judge line for Codex"), INSTALL_CODEX, "INSTALL.md Codex judge line")

    def test_install_remove_folders(self):
        text = section(read("INSTALL.md"), "## Remove")
        for folder in INSTALL_FOLDERS:
            self.assertIn("`" + folder + "`", text)


class MeasurementTests(TextCase):
    """The section ## Measurements of AMENDMENT-11.md: one line for each measured module, as D5 of CON-20261118-19 names it."""

    def setUp(self):
        self.text = section(read("AMENDMENT-11.md"), "## Measurements")

    def test_section_stands_before_the_limits(self):
        text = read("AMENDMENT-11.md")
        self.assertLess(text.index("\n## Measurements\n"), text.index("\n## Limits\n"))

    def test_commit_and_folder(self):
        self.assert_once(self.text, MEASUREMENT_COMMIT, "## Measurements")
        self.assert_once(self.text, MEASUREMENT_FOLDER, "## Measurements")

    def test_one_line_for_each_module(self):
        items = [line for line in self.text.split("\n") if line.startswith("- ")]
        expected = ["- `%s` | commit %s | `%s` sha256 %s | `%s`" % (module, MEASUREMENT_COMMIT[:7], name, digest, result)
                    for module, name, digest, result in MEASUREMENTS]
        self.assertEqual(items, expected)

    def test_result_lines_have_the_form_of_mutate(self):
        form = re.compile(r"mutation result: sites (\d+) \| killed (\d+) \(of these by a time limit: 0\) \| survivors (\d+) "
                          r"\| skipped 0 \| baseline exits \[0(, 0)?\]$")
        for module, _name, digest, result in MEASUREMENTS:
            match = form.match(result)
            self.assertIsNotNone(match, module)
            sites, killed, survivors = (int(match.group(n)) for n in (1, 2, 3))
            self.assertEqual(killed + survivors, sites, module)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_survivors_are_ruled_on(self):
        self.assert_sentences(self.text, MEASUREMENT_SURVIVORS, "## Measurements")


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

    def test_generated_lead_skill_holds_the_mode_table(self):
        self.assert_mode_table(read("portable/skills/readback/SKILL.md"), "portable/skills/readback/SKILL.md")

    def test_generated_lead_skill_holds_the_first_line_forms(self):
        self.assert_first_lines_block(read("portable/skills/readback/SKILL.md"), "portable/skills/readback/SKILL.md")


if __name__ == "__main__":
    unittest.main(failfast=True)
