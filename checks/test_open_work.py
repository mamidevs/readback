#!/usr/bin/env python3
"""Offline checks for scripts/open_work.py (CON-20261123-09, ACC-2 to ACC-4).

Each test builds its records folder in a temporary folder.
Run: python3 -m unittest discover -s checks -p 'test_open_work.py' -v
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "open_work.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "open_work"
MARKER = "<!-- readback lead -->"
KEYS = ["records", "handoffs", "contracts", "unlinked", "legacy_notes", "conflicts"]
STATES = ["accepted", "rejected", "replaced", "judged", "open", "conflict"]
LISTS = ["closed", "withdrawn", "open", "unbound"]

C1, C2, C3 = "CON-20261123-51", "CON-20261123-52", "CON-20261123-53"
H1, H2, H3, H4 = "HND-20261123-61", "HND-20261123-62", "HND-20261123-63", "HND-20261123-64"
N1, N2, N3, N4 = "NOTE-20261123-71", "NOTE-20261123-72", "NOTE-20261123-73", "NOTE-20261123-74"
JUDGE = "JUDGE      PASS | codex:model-a | sample@abc1234 | invented reason"
EXEC = "execution  codex | model-a | high | cli | run-1"
RECEIPT = "receipt    none | no record was stored"


def envelope(rid):
    return '<!-- readback project=sample-0000 scope="shared" record=%s -->\n' % rid


def json_handoff(rid, contract=None, replaces=None, below=None, marker=True):
    obj = {"id": rid, "from": "worker", "date": "2026-11-23", "state": "DONE", "summary": "an invented summary"}
    if contract is not None:
        obj["contract_id"] = contract
    if replaces is not None:
        obj["replaces"] = replaces
    text = envelope(rid) + json.dumps(obj, indent=2) + "\n"
    if below is not None:
        text += (MARKER + "\n" if marker else "") + "\n".join(below) + "\n"
    return text


def text_handoff(rid, contract=None, replaces=None, below=None, marker=True, above=None):
    lines = ["id: " + rid]
    if contract is not None:
        lines.append("contract_id: " + contract)
    if replaces is not None:
        lines.append("replaces: " + replaces)
    lines += ["from: worker", "state: DONE"]
    text = envelope(rid) + "\n".join(lines) + "\n"
    if above:
        text += "\n".join(above) + "\n"
    if below is not None:
        text += (MARKER + "\n" if marker else "") + "\n".join(below) + "\n"
    return text


def contract(rid):
    return envelope(rid) + "%s  An invented contract.\nACC-1  Something holds.  Check: true\n" % rid


def review_note(rid, handoff, con):
    return envelope(rid) + "\n".join(["kind review", "handoff " + handoff, "contract " + con, JUDGE, EXEC, RECEIPT]) + "\n"


def acceptance_note(rid, handoff, con, answer="yes", review="none"):
    accepted = "ACCEPTED %s | 2026-11-23" % answer + (" | invented reason" if answer == "no" else "")
    return envelope(rid) + "\n".join(["kind acceptance", "handoff " + handoff, "contract " + con, "review " + review, accepted]) + "\n"


def withdrawal_note(rid, con, replaced_by="none"):
    return envelope(rid) + "\n".join(["kind withdrawal", "contract " + con, "replaced-by " + replaced_by,
                                      "date 2026-11-23", "reason invented"]) + "\n"


def free_note(rid, first, body=()):
    return envelope(rid) + first + "\n\n" + "\n".join(body) + "\n"


def run_script(*args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT)] + [str(a) for a in args], capture_output=True, text=True,
                          timeout=60, cwd=cwd)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "records"
        self.dir.mkdir()

    def folder(self, records):
        for rid, text in records.items():
            data = text if isinstance(text, bytes) else text.encode("utf-8")
            (self.dir / (rid + ".md")).write_bytes(data)

    def result(self, records=None):
        if records is not None:
            self.folder(records)
        res = run_script("--records", self.dir)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        obj = json.loads(res.stdout)
        self.assertEqual(list(obj), KEYS)
        self.assertEqual(list(obj["handoffs"]), STATES)
        self.assertEqual(list(obj["contracts"]), LISTS)
        return obj

    def states(self, records):
        """The state of each handoff, and a check that each stands in exactly one list."""
        obj = self.result(records)
        found = {}
        for state in STATES:
            for rid in obj["handoffs"][state]:
                self.assertNotIn(rid, found)
                found[rid] = state
        wanted = sorted(r for r in records if r.startswith("HND-") and isinstance(records[r], str))
        self.assertEqual(sorted(found), wanted)
        return found

    def lists(self, records):
        obj = self.result(records)
        found = {}
        for name in LISTS:
            for rid in obj["contracts"][name]:
                self.assertNotIn(rid, found)
                found[rid] = name
        wanted = sorted(r for r in records if r.startswith("CON-") and isinstance(records[r], str))
        self.assertEqual(sorted(found), wanted)
        return found

    def conflicts(self, records):
        return self.result(records)["conflicts"]


class HandoffStateTests(Base):
    def test_open_when_no_source_gives_another_state(self):
        self.assertEqual(self.states({H1: json_handoff(H1, C1)}), {H1: "open"})

    def test_judged_from_an_embedded_judge_line_with_the_lead_marker(self):
        self.assertEqual(self.states({H1: json_handoff(H1, C1, below=[JUDGE, EXEC, RECEIPT])}), {H1: "judged"})

    def test_judged_from_an_embedded_judge_line_without_the_marker(self):
        self.assertEqual(self.states({H1: text_handoff(H1, C1, below=[JUDGE], marker=False)}), {H1: "judged"})

    def test_judged_from_a_review_note(self):
        self.assertEqual(self.states({H1: json_handoff(H1, C1), N1: review_note(N1, H1, C1)}), {H1: "judged"})

    def test_judged_from_an_attributed_free_form_note(self):
        note = free_note(N1, "# Judge call for %s" % H1, [JUDGE])
        self.assertEqual(self.states({H1: json_handoff(H1, C1), N1: note}), {H1: "judged"})

    def test_accepted_from_an_embedded_line_with_the_marker(self):
        self.assertEqual(self.states({H1: json_handoff(H1, C1, below=["ACCEPTED yes | 2026-11-23 | reason"])}), {H1: "accepted"})

    def test_accepted_from_an_embedded_line_without_the_marker(self):
        self.assertEqual(self.states({H1: text_handoff(H1, C1, below=["ACCEPTED yes | 2026-11-23"], marker=False)}), {H1: "accepted"})

    def test_accepted_from_an_acceptance_note(self):
        self.assertEqual(self.states({H1: json_handoff(H1, C1), N1: acceptance_note(N1, H1, C1)}), {H1: "accepted"})

    def test_accepted_from_an_attributed_free_form_note(self):
        note = free_note(N1, "# Verdict for %s" % H1, [JUDGE, "ACCEPTED yes | 2026-11-23"])
        self.assertEqual(self.states({H1: json_handoff(H1, C1), N1: note}), {H1: "accepted"})

    def test_rejected_from_an_embedded_line_with_the_marker(self):
        self.assertEqual(self.states({H1: json_handoff(H1, C1, below=["ACCEPTED no | 2026-11-23 | reason"])}), {H1: "rejected"})

    def test_rejected_from_an_embedded_line_without_the_marker(self):
        self.assertEqual(self.states({H1: text_handoff(H1, C1, below=["ACCEPTED no | 2026-11-23 | r"], marker=False)}), {H1: "rejected"})

    def test_rejected_from_an_acceptance_note_with_no(self):
        self.assertEqual(self.states({H1: json_handoff(H1, C1), N1: acceptance_note(N1, H1, C1, "no")}), {H1: "rejected"})

    def test_rejected_from_an_attributed_free_form_note(self):
        note = free_note(N1, "# Verdict for %s" % H1, ["ACCEPTED no | 2026-11-23 | r"])
        self.assertEqual(self.states({H1: json_handoff(H1, C1), N1: note}), {H1: "rejected"})

    def test_replaced_from_the_replaces_of_another_handoff(self):
        found = self.states({H1: json_handoff(H1, C1), H2: json_handoff(H2, C1, replaces=H1)})
        self.assertEqual(found, {H1: "replaced", H2: "open"})

    def test_replaced_from_a_text_form_handoff(self):
        found = self.states({H1: json_handoff(H1, C1), H2: text_handoff(H2, C1, replaces=H1)})
        self.assertEqual(found[H1], "replaced")

    def test_replaces_value_is_trimmed(self):
        found = self.states({H1: json_handoff(H1, C1), H2: json_handoff(H2, C1, replaces="  %s\n" % H1)})
        self.assertEqual(found[H1], "replaced")

    def test_replaces_none_replaces_nothing(self):
        found = self.states({H1: json_handoff(H1, C1), H2: json_handoff(H2, C1, replaces="none")})
        self.assertEqual(found, {H1: "open", H2: "open"})

    def test_conflict_from_a_yes_and_a_no(self):
        found = self.states({H1: json_handoff(H1, C1, below=["ACCEPTED yes | 2026-11-23"]), N1: acceptance_note(N1, H1, C1, "no")})
        self.assertEqual(found, {H1: "conflict"})

    def test_conflict_from_a_yes_that_another_handoff_replaces(self):
        found = self.states({H1: json_handoff(H1, C1, below=["ACCEPTED yes | 2026-11-23"]), H2: json_handoff(H2, C1, replaces=H1)})
        self.assertEqual(found, {H1: "conflict", H2: "open"})

    def test_order_conflict_comes_before_accepted(self):
        found = self.states({H1: json_handoff(H1, C1, below=["ACCEPTED yes | 2026-11-23"]), H2: json_handoff(H2, C1, replaces=H1)})
        self.assertNotEqual(found[H1], "accepted")

    def test_order_rejected_comes_before_replaced(self):
        found = self.states({H1: json_handoff(H1, C1, below=["ACCEPTED no | 2026-11-23 | r"]), H2: json_handoff(H2, C1, replaces=H1)})
        self.assertEqual(found[H1], "rejected")

    def test_order_replaced_comes_before_judged(self):
        found = self.states({H1: json_handoff(H1, C1, below=[JUDGE]), H2: json_handoff(H2, C1, replaces=H1)})
        self.assertEqual(found[H1], "replaced")

    def test_order_accepted_comes_before_judged(self):
        found = self.states({H1: json_handoff(H1, C1, below=[JUDGE, "ACCEPTED yes | 2026-11-23"])})
        self.assertEqual(found[H1], "accepted")

    def test_acceptance_note_with_no_alone_gives_rejected_and_leaves_the_contract_open(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1), N1: acceptance_note(N1, H1, C1, "no")}
        self.assertEqual(self.states(records), {H1: "rejected"})
        self.assertEqual(self.lists(records), {C1: "open"})

    def test_the_last_embedded_line_wins(self):
        for below, state in ((["ACCEPTED no | d | r", "ACCEPTED yes | d"], "accepted"),
                             (["ACCEPTED yes | d", "ACCEPTED no | d | r"], "rejected")):
            with self.subTest(below):
                self.assertEqual(self.states({H1: json_handoff(H1, C1, below=below)}), {H1: state})
                self.dir.joinpath(H1 + ".md").unlink()

    def test_a_line_above_the_marker_is_ignored(self):
        text = text_handoff(H1, C1, above=[JUDGE, "ACCEPTED yes | 2026-11-23"], below=["no line here"])
        self.assertEqual(self.states({H1: text}), {H1: "open"})

    def test_only_lines_below_the_first_marker_count(self):
        text = text_handoff(H1, C1, below=[JUDGE, MARKER, "ACCEPTED yes | d"])
        self.assertEqual(self.states({H1: text}), {H1: "accepted"})

    def test_a_tab_after_the_word_counts(self):
        text = text_handoff(H1, C1, below=["JUDGE\tPASS | a | b | c", "ACCEPTED\tyes | d"])
        self.assertEqual(self.states({H1: text}), {H1: "accepted"})

    def test_an_indented_or_longer_word_does_not_count(self):
        for line in ("  JUDGE PASS | a | b | c", "\tJUDGE PASS", "JUDGES PASS", "JUDGE", "judge PASS", "- JUDGE PASS"):
            with self.subTest(line):
                self.assertEqual(self.states({H1: text_handoff(H1, C1, below=[line])}), {H1: "open"})
        for line in ("  ACCEPTED yes | d", "ACCEPTEDyes", "ACCEPTED"):
            with self.subTest(line):
                self.assertEqual(self.states({H1: text_handoff(H1, C1, below=[line])}), {H1: "open"})

    def test_another_first_field_is_no_decision(self):
        for line in ("ACCEPTED maybe | 2026-11-23", "ACCEPTED YES | d", "ACCEPTED yes, with a note | d", "ACCEPTED | d", "ACCEPTED  "):
            with self.subTest(line):
                self.assertEqual(self.states({H1: text_handoff(H1, C1, below=[line])}), {H1: "open"})

    def test_a_no_decision_line_does_not_hide_the_judge_line(self):
        found = self.states({H1: text_handoff(H1, C1, below=[JUDGE, "ACCEPTED maybe | d"])})
        self.assertEqual(found, {H1: "judged"})

    def test_an_acceptance_note_that_check_rejects_accepts_nothing(self):
        bad = acceptance_note(N1, H1, C1).replace("2026-11-23", "soon")
        self.assertEqual(self.states({H1: json_handoff(H1, C1), N1: bad}), {H1: "open"})

    def test_a_review_note_gives_no_decision(self):
        found = self.states({H1: json_handoff(H1, C1), N1: review_note(N1, H1, C1)})
        self.assertEqual(found, {H1: "judged"})

    def test_a_note_for_one_handoff_leaves_the_other_open(self):
        found = self.states({H1: json_handoff(H1, C1), H2: json_handoff(H2, C1), N1: acceptance_note(N1, H1, C1)})
        self.assertEqual(found, {H1: "accepted", H2: "open"})

    def test_a_handoff_that_replaces_itself_is_not_replaced(self):
        self.assertEqual(self.states({H1: json_handoff(H1, C1, replaces=H1)}), {H1: "open"})

    def test_a_handoff_that_parse_handoff_rejects_has_no_link_keys(self):
        bad = envelope(H2) + "{ not json\n"
        records = {H1: json_handoff(H1, C1), H2: bad}
        self.assertEqual(self.states(records), {H1: "open", H2: "open"})
        self.assertEqual(self.result()["unlinked"], [H2])
        with self.subTest("its replaces is inert"):
            bad_replaces = envelope(H2) + '{"replaces": "%s", "contract_id": "%s",}\n' % (H1, C1)
            self.assertEqual(self.states({H1: json_handoff(H1, C1), H2: bad_replaces}), {H1: "open", H2: "open"})

    def test_a_replacement_chain(self):
        records = {H1: json_handoff(H1, C1, below=[JUDGE]), H2: json_handoff(H2, C1, replaces=H1),
                   H3: json_handoff(H3, C1, replaces=H2, below=["ACCEPTED yes | d"])}
        self.assertEqual(self.states(records), {H1: "replaced", H2: "replaced", H3: "accepted"})


class ContractListTests(Base):
    def test_closed_when_an_accepted_handoff_holds_its_id(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1, below=["ACCEPTED yes | d"])}
        self.assertEqual(self.lists(records), {C1: "closed"})

    def test_withdrawn_when_a_withdrawal_note_names_it(self):
        self.assertEqual(self.lists({C1: contract(C1), N1: withdrawal_note(N1, C1)}), {C1: "withdrawn"})

    def test_open_when_a_handoff_holds_its_id(self):
        self.assertEqual(self.lists({C1: contract(C1), H1: json_handoff(H1, C1)}), {C1: "open"})

    def test_unbound_when_no_handoff_holds_its_id(self):
        self.assertEqual(self.lists({C1: contract(C1)}), {C1: "unbound"})

    def test_each_contract_stands_in_exactly_one_list(self):
        records = {C1: contract(C1), C2: contract(C2), C3: contract(C3),
                   H1: json_handoff(H1, C1, below=["ACCEPTED yes | d"]), H2: json_handoff(H2, C2),
                   N1: withdrawal_note(N1, C3)}
        self.assertEqual(self.lists(records), {C1: "closed", C2: "open", C3: "withdrawn"})
        obj = self.result()
        self.assertEqual(obj["contracts"], {"closed": [C1], "withdrawn": [C3], "open": [C2], "unbound": []})

    def test_closed_comes_before_withdrawn(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1, below=["ACCEPTED yes | d"]), N1: withdrawal_note(N1, C1)}
        self.assertEqual(self.lists(records), {C1: "closed"})

    def test_withdrawn_comes_before_open(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1), N1: withdrawal_note(N1, C1)}
        self.assertEqual(self.lists(records), {C1: "withdrawn"})

    def test_a_handoff_in_the_state_conflict_does_not_close_its_contract(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1, below=["ACCEPTED yes | d"]), H2: json_handoff(H2, C1, replaces=H1)}
        self.assertEqual(self.states(records)[H1], "conflict")
        self.assertEqual(self.lists(records), {C1: "open"})

    def test_a_rejected_replaced_or_judged_handoff_does_not_close_its_contract(self):
        for below in (["ACCEPTED no | d | r"], [JUDGE], None):
            with self.subTest(below):
                records = {C1: contract(C1), H1: json_handoff(H1, C1, below=below)}
                self.assertEqual(self.lists(records), {C1: "open"})
        records = {C1: contract(C1), H1: json_handoff(H1, C1), H2: json_handoff(H2, C2, replaces=H1)}
        self.assertEqual(self.lists(records), {C1: "open"})

    def test_an_accepted_handoff_for_another_contract_closes_only_that_one(self):
        records = {C1: contract(C1), C2: contract(C2), H1: json_handoff(H1, C2, below=["ACCEPTED yes | d"])}
        self.assertEqual(self.lists(records), {C1: "unbound", C2: "closed"})

    def test_contract_id_is_trimmed_and_read_in_the_text_form(self):
        records = {C1: contract(C1), H1: text_handoff(H1, "  %s  " % C1, below=["ACCEPTED yes | d"])}
        self.assertEqual(self.lists(records), {C1: "closed"})

    def test_a_contract_id_that_matches_no_record_binds_nothing(self):
        records = {C1: contract(C1), H1: json_handoff(H1, "see %s" % C1)}
        self.assertEqual(self.lists(records), {C1: "unbound"})

    def test_a_closure_note_alone_does_not_bind_a_contract(self):
        records = {C1: contract(C1), H1: json_handoff(H1), N1: acceptance_note(N1, H1, C1)}
        self.assertEqual(self.lists(records), {C1: "unbound"})

    def test_a_withdrawal_note_that_check_rejects_withdraws_nothing(self):
        bad = withdrawal_note(N1, C1, C1)
        self.assertEqual(self.lists({C1: contract(C1), N1: bad}), {C1: "unbound"})

    def test_a_replaced_handoff_still_holds_the_contract_open(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1, below=[JUDGE]), H2: json_handoff(H2, None, replaces=H1)}
        self.assertEqual(self.lists(records), {C1: "open"})


class UnlinkedTests(Base):
    def test_unlinked_lists_each_handoff_without_contract_id(self):
        records = {H1: json_handoff(H1), H2: json_handoff(H2, C1), H3: text_handoff(H3), H4: json_handoff(H4, "   ")}
        self.assertEqual(self.result(records)["unlinked"], [H1, H3, H4])

    def test_a_handoff_without_contract_id_still_has_a_state(self):
        self.assertEqual(self.states({H1: json_handoff(H1, below=["ACCEPTED yes | d"])}), {H1: "accepted"})

    def test_unlinked_is_empty_when_every_handoff_has_one(self):
        self.assertEqual(self.result({H1: json_handoff(H1, C1)})["unlinked"], [])


class ConflictTests(Base):
    def test_decision(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1, below=["ACCEPTED yes | d"]), N1: acceptance_note(N1, H1, C1, "no")}
        self.assertEqual(self.conflicts(records), [{"kind": "decision", "ids": [H1]}])

    def test_decision_from_two_acceptance_notes(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1), N1: acceptance_note(N1, H1, C1), N2: acceptance_note(N2, H1, C1, "no")}
        self.assertEqual(self.conflicts(records), [{"kind": "decision", "ids": [H1]}])

    def test_accepted_and_replaced(self):
        records = {H1: json_handoff(H1, C1, below=["ACCEPTED yes | d"]), H2: json_handoff(H2, C1, replaces=H1)}
        self.assertEqual(self.conflicts(records), [{"kind": "accepted-and-replaced", "ids": [H1, H2]}])

    def test_missing_target_of_a_replaces_value(self):
        self.assertEqual(self.conflicts({H1: json_handoff(H1, C1, replaces=H4)}),
                         [{"kind": "missing-target", "ids": [H1, H4]}])

    def test_missing_target_of_the_handoff_of_a_review_note(self):
        self.assertEqual(self.conflicts({C1: contract(C1), N1: review_note(N1, H1, C1)}),
                         [{"kind": "missing-target", "ids": [N1, H1]}])

    def test_missing_target_of_the_handoff_of_an_acceptance_note(self):
        self.assertEqual(self.conflicts({C1: contract(C1), N1: acceptance_note(N1, H1, C1)}),
                         [{"kind": "missing-target", "ids": [N1, H1]}])

    def test_missing_target_of_the_contract_of_a_review_note(self):
        self.assertEqual(self.conflicts({H1: json_handoff(H1), N1: review_note(N1, H1, C1)}),
                         [{"kind": "missing-target", "ids": [N1, C1]}])

    def test_missing_target_of_the_contract_of_an_acceptance_note(self):
        self.assertEqual(self.conflicts({H1: json_handoff(H1), N1: acceptance_note(N1, H1, C1)}),
                         [{"kind": "missing-target", "ids": [N1, C1]}])

    def test_missing_target_of_the_review_of_an_acceptance_note(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1), N1: acceptance_note(N1, H1, C1, review=N4)}
        self.assertEqual(self.conflicts(records), [{"kind": "missing-target", "ids": [N1, N4]}])

    def test_missing_target_of_the_contract_of_a_withdrawal_note(self):
        self.assertEqual(self.conflicts({N1: withdrawal_note(N1, C1)}), [{"kind": "missing-target", "ids": [N1, C1]}])

    def test_missing_target_of_the_replaced_by_of_a_withdrawal_note(self):
        self.assertEqual(self.conflicts({C1: contract(C1), N1: withdrawal_note(N1, C1, C2)}),
                         [{"kind": "missing-target", "ids": [N1, C2]}])

    def test_none_is_no_missing_target(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1, replaces="none"), N1: withdrawal_note(N1, C1, "none"),
                   N2: acceptance_note(N2, H1, C1, "no", review="none")}
        self.assertEqual(self.conflicts(records), [])

    def test_a_target_that_exists_is_no_missing_target(self):
        records = {C1: contract(C1), C2: contract(C2), H1: json_handoff(H1, C1), H2: json_handoff(H2, C1, replaces=H1),
                   N1: review_note(N1, H1, C1), N2: acceptance_note(N2, H1, C1, "no", review=N1), N3: withdrawal_note(N3, C2, C1)}
        self.assertEqual(self.conflicts(records), [])

    def test_a_missing_target_is_listed_once_for_each_source_and_value(self):
        records = {C1: contract(C1), N1: withdrawal_note(N1, C2, C3), N2: withdrawal_note(N2, C2, "none")}
        self.assertEqual(self.conflicts(records), [{"kind": "missing-target", "ids": [N1, C2]},
                                                   {"kind": "missing-target", "ids": [N1, C3]},
                                                   {"kind": "missing-target", "ids": [N2, C2]}])

    def test_contract_mismatch_of_a_review_note(self):
        records = {C1: contract(C1), C2: contract(C2), H1: json_handoff(H1, C1), N1: review_note(N1, H1, C2)}
        self.assertEqual(self.conflicts(records), [{"kind": "contract-mismatch", "ids": [N1, H1]}])

    def test_contract_mismatch_of_an_acceptance_note(self):
        records = {C1: contract(C1), C2: contract(C2), H1: json_handoff(H1, C1), N1: acceptance_note(N1, H1, C2)}
        self.assertEqual(self.conflicts(records), [{"kind": "contract-mismatch", "ids": [N1, H1]}])

    def test_no_contract_mismatch_when_the_contracts_agree_or_the_handoff_has_none(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1), H2: json_handoff(H2), N1: review_note(N1, H1, C1),
                   N2: acceptance_note(N2, H2, C1)}
        self.assertEqual(self.conflicts(records), [])

    def test_withdrawn_and_closed(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1, below=["ACCEPTED yes | d"]), N1: withdrawal_note(N1, C1)}
        self.assertEqual(self.conflicts(records), [{"kind": "withdrawn-and-closed", "ids": [N1, C1]}])

    def test_no_withdrawn_and_closed_for_a_withdrawn_contract(self):
        self.assertEqual(self.conflicts({C1: contract(C1), H1: json_handoff(H1, C1), N1: withdrawal_note(N1, C1)}), [])

    def test_invalid_note(self):
        bad = review_note(N1, H1, C1).replace("kind review", "kind review\nkind review")
        self.assertEqual(self.conflicts({C1: contract(C1), H1: json_handoff(H1, C1), N1: bad}), [{"kind": "invalid-note", "ids": [N1]}])

    def test_invalid_note_is_used_for_nothing(self):
        bad = review_note(N1, H1, C1).replace("handoff " + H1, "handoff " + H1 + "\nhandoff " + H2)
        records = {C1: contract(C1), H1: json_handoff(H1, C1), N1: bad}
        self.assertEqual(self.states(records), {H1: "open"})

    def test_an_unknown_kind_line_is_an_invalid_note(self):
        note = envelope(N1) + "kind opinion\nhandoff %s\n" % H1
        self.assertEqual(self.conflicts({H1: json_handoff(H1, C1), N1: note}), [{"kind": "invalid-note", "ids": [N1]}])

    def test_unreadable_record_is_counted_and_used_for_nothing_else(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1), H2: b"\xff\xfe\x00 not text", C2: b"\xc3\x28", N1: b"\x80"}
        obj = self.result(records)
        self.assertEqual(obj["records"], {"CON": 2, "HND": 2, "NOTE": 1})
        self.assertEqual(obj["conflicts"], [{"kind": "unreadable", "ids": [C2]}, {"kind": "unreadable", "ids": [H2]},
                                            {"kind": "unreadable", "ids": [N1]}])
        self.assertEqual(obj["handoffs"]["open"], [H1])
        self.assertEqual(sum(len(v) for v in obj["handoffs"].values()), 1)
        self.assertEqual(obj["contracts"], {"closed": [], "withdrawn": [], "open": [C1], "unbound": []})
        self.assertEqual(obj["unlinked"], [])

    def test_an_unreadable_record_is_a_target_that_exists(self):
        records = {H1: json_handoff(H1, C1, replaces=H2), H2: b"\xff"}
        self.assertEqual(self.conflicts(records), [{"kind": "unreadable", "ids": [H2]}])

    def test_the_script_lists_a_conflict_and_resolves_none(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1, below=["ACCEPTED yes | d"]), N1: acceptance_note(N1, H1, C1, "no")}
        obj = self.result(records)
        self.assertEqual(obj["handoffs"]["conflict"], [H1])
        self.assertEqual(obj["contracts"]["open"], [C1])

    def test_conflicts_are_sorted_by_kind_then_ids(self):
        records = {C1: contract(C1), H1: json_handoff(H1, C1, replaces=H4, below=["ACCEPTED yes | d"]),
                   H2: json_handoff(H2, C1, replaces=H1), N1: withdrawal_note(N1, C2), H3: b"\xff"}
        kinds = [(c["kind"], c["ids"]) for c in self.conflicts(records)]
        self.assertEqual(kinds, sorted(kinds))
        self.assertEqual(len(kinds), 4)


class LegacyNoteTests(Base):
    def test_attributed_note_is_listed_with_its_handoff(self):
        note = free_note(N1, "# Judge result for %s" % H1, [JUDGE])
        obj = self.result({H1: json_handoff(H1, C1), N1: note})
        self.assertEqual(obj["legacy_notes"], [{"id": N1, "handoffs": [H1], "attributed": True}])

    def test_first_line_after_the_envelope_and_blank_lines(self):
        note = envelope(N1) + "\n\n# About %s\n%s\n" % (H1, JUDGE)
        self.assertEqual(self.result({H1: json_handoff(H1, C1), N1: note})["legacy_notes"],
                         [{"id": N1, "handoffs": [H1], "attributed": True}])

    def test_two_handoff_ids_in_the_first_line_change_no_state(self):
        note = free_note(N1, "# Verdicts for %s and %s" % (H1, H2), [JUDGE, "ACCEPTED yes | d"])
        records = {H1: json_handoff(H1, C1), H2: json_handoff(H2, C1), N1: note}
        self.assertEqual(self.states(records), {H1: "open", H2: "open"})
        self.assertEqual(self.result()["legacy_notes"], [{"id": N1, "handoffs": [H1, H2], "attributed": False}])

    def test_no_handoff_id_in_the_first_line_changes_no_state(self):
        note = free_note(N1, "# Round notes", ["Looked at %s." % H1, JUDGE, "ACCEPTED yes | d"])
        records = {H1: json_handoff(H1, C1), N1: note}
        self.assertEqual(self.states(records), {H1: "open"})
        self.assertEqual(self.result()["legacy_notes"], [{"id": N1, "handoffs": [], "attributed": False}])

    def test_a_first_line_that_does_not_start_with_a_hash_changes_no_state(self):
        note = free_note(N1, "Verdict for %s" % H1, [JUDGE, "ACCEPTED yes | d"])
        self.assertEqual(self.states({H1: json_handoff(H1, C1), N1: note}), {H1: "open"})
        self.assertEqual(self.result()["legacy_notes"], [{"id": N1, "handoffs": [H1], "attributed": False}])

    def test_the_same_id_twice_in_the_first_line_names_one_handoff(self):
        note = free_note(N1, "# %s again %s" % (H1, H1), [JUDGE])
        self.assertEqual(self.states({H1: json_handoff(H1, C1), N1: note}), {H1: "judged"})

    def test_ids_that_are_not_handoff_ids_are_not_named(self):
        for first in ("# %s" % C1, "# HND-20261123-1", "# HND-20261123-1234567", "# xHND-20261123-61", "# hnd-20261123-61"):
            with self.subTest(first):
                note = free_note(N1, first, [JUDGE])
                self.assertEqual(self.result({H1: json_handoff(H1, C1), N1: note})["legacy_notes"],
                                 [{"id": N1, "handoffs": [], "attributed": False}])
                self.dir.joinpath(N1 + ".md").unlink()

    def test_a_note_without_a_judge_line_is_not_listed(self):
        for first in ("# Remarks on %s" % H1, "# Remarks", "Plain words"):
            with self.subTest(first):
                note = free_note(N1, first, ["Nothing to see."])
                self.assertEqual(self.result({H1: json_handoff(H1, C1), N1: note})["legacy_notes"], [])
                self.dir.joinpath(N1 + ".md").unlink()

    def test_an_attributed_note_with_only_a_decision_is_applied_and_not_listed(self):
        note = free_note(N1, "# Decision for %s" % H1, ["ACCEPTED yes | 2026-11-23"])
        records = {H1: json_handoff(H1, C1), N1: note}
        self.assertEqual(self.states(records), {H1: "accepted"})
        self.assertEqual(self.result()["legacy_notes"], [])

    def test_an_unattributed_note_gives_no_decision(self):
        note = free_note(N1, "# Round", ["ACCEPTED yes | 2026-11-23"])
        self.assertEqual(self.states({H1: json_handoff(H1, C1), N1: note}), {H1: "open"})

    def test_the_last_embedded_lines_of_a_note_count(self):
        note = free_note(N1, "# Verdict for %s" % H1, ["ACCEPTED yes | d", "ACCEPTED no | d | r"])
        self.assertEqual(self.states({H1: json_handoff(H1, C1), N1: note}), {H1: "rejected"})

    def test_only_lines_below_the_marker_of_a_note_count(self):
        note = free_note(N1, "# Verdict for %s" % H1, [JUDGE, MARKER, "nothing"])
        self.assertEqual(self.states({H1: json_handoff(H1, C1), N1: note}), {H1: "open"})
        self.assertEqual(self.result()["legacy_notes"], [])

    def test_an_attributed_note_for_an_absent_handoff_is_listed_and_changes_nothing(self):
        note = free_note(N1, "# Verdict for %s" % H4, [JUDGE])
        obj = self.result({H1: json_handoff(H1, C1), N1: note})
        self.assertEqual(obj["legacy_notes"], [{"id": N1, "handoffs": [H4], "attributed": True}])
        self.assertEqual(obj["handoffs"]["open"], [H1])
        self.assertEqual(obj["conflicts"], [])

    def test_a_note_with_a_kind_line_is_not_free_form(self):
        note = free_note(N1, "# Verdict for %s" % H1, ["kind of odd", JUDGE])
        obj = self.result({H1: json_handoff(H1, C1), N1: note})
        self.assertEqual(obj["legacy_notes"], [])
        self.assertEqual(obj["conflicts"], [{"kind": "invalid-note", "ids": [N1]}])

    def test_legacy_notes_are_sorted_by_id(self):
        records = {H1: json_handoff(H1, C1), N2: free_note(N2, "# R", [JUDGE]), N1: free_note(N1, "# R", [JUDGE])}
        self.assertEqual([n["id"] for n in self.result(records)["legacy_notes"]], [N1, N2])


class RecordAndOutputTests(Base):
    def test_records_holds_the_number_of_records_of_each_kind(self):
        records = {C1: contract(C1), C2: contract(C2), H1: json_handoff(H1, C1), N1: free_note(N1, "# x"),
                   N2: free_note(N2, "# x"), N3: free_note(N3, "# x")}
        self.assertEqual(self.result(records)["records"], {"CON": 2, "HND": 1, "NOTE": 3})

    def test_an_empty_folder(self):
        obj = self.result({})
        self.assertEqual(obj["records"], {"CON": 0, "HND": 0, "NOTE": 0})
        self.assertEqual(obj["handoffs"], {s: [] for s in STATES})
        self.assertEqual(obj["contracts"], {n: [] for n in LISTS})
        self.assertEqual((obj["unlinked"], obj["legacy_notes"], obj["conflicts"]), ([], [], []))

    def test_only_a_file_id_dot_md_directly_in_the_folder_is_a_record(self):
        self.folder({H1: json_handoff(H1, C1)})
        not_records = ["README.md", "CON-1-1.md", "CON-20261123-1.md", "CON-20261123-1234567.md", "HND-2026112-12.md",
                       "hnd-20261123-12.md", "HND-20261123-12.txt", "HND-20261123-12.md.bak", "HND-20261123-12", "XHND-20261123-12.md",
                       "HND-20261123-12 .md", "TASK-20261123-12.md", "NOTE-20261123-12.MD",
                       "CON-٢٠٢٦١١٢٣-12.md", "HND-20261123-12٣.md"]
        for name in not_records:
            (self.dir / name).write_text(json_handoff("HND-20261123-12"), encoding="utf-8")
        (self.dir / "HND-20261123-13.md").mkdir()
        sub = self.dir / "sub"
        sub.mkdir()
        (sub / "HND-20261123-14.md").write_text(json_handoff("HND-20261123-14"), encoding="utf-8")
        obj = self.result()
        self.assertEqual(obj["records"], {"CON": 0, "HND": 1, "NOTE": 0})
        self.assertEqual(obj["handoffs"]["open"], [H1])

    def test_a_symbolic_link_is_not_read(self):
        outside = Path(self.tmp.name) / "outside.md"
        outside.write_text(json_handoff(H2, C1), encoding="utf-8")
        (self.dir / (H2 + ".md")).symlink_to(outside)
        self.assertEqual(self.result({H1: json_handoff(H1, C1)})["records"]["HND"], 1)

    def test_lists_are_sorted_by_id_in_string_order(self):
        ids = ["HND-20261123-10", "HND-20261123-02", "HND-20261123-100", "HND-20260101-99"]
        records = {rid: json_handoff(rid, C1) for rid in ids}
        obj = self.result(records)
        self.assertEqual(obj["handoffs"]["open"], sorted(ids))
        self.assertEqual(obj["unlinked"], [])

    def test_two_runs_print_the_same_bytes(self):
        records = {C1: contract(C1), C2: contract(C2), H1: json_handoff(H1, C1, below=["ACCEPTED yes | d"]),
                   H2: json_handoff(H2, C1, replaces=H1), H3: b"\xff", N1: acceptance_note(N1, H1, C1, "no"),
                   N2: free_note(N2, "# R", [JUDGE]), N3: withdrawal_note(N3, C2, C3)}
        self.folder(records)
        first = run_script("--records", self.dir)
        second = run_script("--records", self.dir)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stdout.count("\n"), 1)
        self.assertTrue(first.stdout.endswith("\n"))

    def snapshot(self):
        return {str(p.relative_to(self.dir)): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                for p in sorted(self.dir.rglob("*"))}

    def test_no_file_is_written(self):
        self.folder({C1: contract(C1), H1: json_handoff(H1, C1, below=[JUDGE]), H2: b"\xff", N1: free_note(N1, "# R", [JUDGE]),
                     N2: withdrawal_note(N2, C1)})
        (self.dir / "sub").mkdir()
        before = (sorted(os.listdir(self.dir)), self.snapshot())
        scripts_before = sorted(os.listdir(ROOT / "scripts"))
        self.assertEqual(run_script("--records", self.dir).returncode, 0)
        self.assertEqual((sorted(os.listdir(self.dir)), self.snapshot()), before)
        self.assertEqual(sorted(os.listdir(ROOT / "scripts")), scripts_before)

    def test_no_file_is_written_in_the_working_folder(self):
        self.folder({H1: json_handoff(H1, C1)})
        work = Path(self.tmp.name) / "work"
        work.mkdir()
        self.assertEqual(run_script("--records", self.dir, cwd=work).returncode, 0)
        self.assertEqual(os.listdir(work), [])

    def test_the_script_run_as_a_command(self):
        self.folder({C1: contract(C1), H1: json_handoff(H1, C1)})
        res = subprocess.run([sys.executable, str(SCRIPT), "--records", str(self.dir)], capture_output=True, text=True,
                             timeout=60, cwd=self.tmp.name)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertEqual(list(json.loads(res.stdout)), KEYS)

    def test_a_relative_folder_works(self):
        self.folder({H1: json_handoff(H1, C1)})
        res = run_script("--records", "records", cwd=self.tmp.name)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_a_crlf_record(self):
        text = json_handoff(H1, C1, below=[JUDGE, "ACCEPTED yes | d"]).replace("\n", "\r\n")
        self.assertEqual(self.states({H1: text}), {H1: "accepted"})


class ExitTwoTests(Base):
    def assert_refused(self, *args):
        res = run_script(*args)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertEqual(res.stdout, "")
        self.assertEqual(len(res.stderr.rstrip("\n").split("\n")), 1, res.stderr)
        self.assertTrue(res.stderr.endswith("\n"))

    def test_a_missing_folder(self):
        self.assert_refused("--records", Path(self.tmp.name) / "absent")

    def test_a_path_that_is_a_file(self):
        path = Path(self.tmp.name) / "file.md"
        path.write_text("x", encoding="utf-8")
        self.assert_refused("--records", path)

    def test_an_empty_folder_argument(self):
        self.assert_refused("--records", "")

    def test_no_argument(self):
        self.assert_refused()

    def test_an_option_without_a_value(self):
        self.assert_refused("--records")

    def test_an_unknown_option(self):
        self.assert_refused("--records", self.dir, "--other", "x")

    def test_a_stray_argument(self):
        self.assert_refused("--records", self.dir, "stray")
        self.assert_refused(self.dir)

    def test_the_option_twice(self):
        self.assert_refused("--records", self.dir, "--records", self.dir)

    def test_an_abbreviated_option(self):
        self.assert_refused("--rec", self.dir)

    def test_a_folder_that_cannot_be_listed(self):
        if os.geteuid() == 0:
            self.skipTest("root lists any folder")
        locked = Path(self.tmp.name) / "locked"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        self.assert_refused("--records", locked)


EMBEDDED_EXPECTED = {
    "records": {"CON": 3, "HND": 7, "NOTE": 0},
    "handoffs": {"accepted": ["HND-20261123-60", "HND-20261123-66"], "rejected": ["HND-20261123-63"],
                 "replaced": ["HND-20261123-62"], "judged": ["HND-20261123-61"],
                 "open": ["HND-20261123-64", "HND-20261123-65"], "conflict": []},
    "contracts": {"closed": ["CON-20261123-51", "CON-20261123-53"], "withdrawn": [], "open": ["CON-20261123-52"], "unbound": []},
    "unlinked": ["HND-20261123-65"],
    "legacy_notes": [],
    "conflicts": [],
}
NOTES_EXPECTED = {
    "records": {"CON": 3, "HND": 3, "NOTE": 6},
    "handoffs": {"accepted": ["HND-20261123-80"], "rejected": [], "replaced": [], "judged": ["HND-20261123-82"],
                 "open": ["HND-20261123-81"], "conflict": []},
    "contracts": {"closed": ["CON-20261123-71"], "withdrawn": ["CON-20261123-72"], "open": ["CON-20261123-73"], "unbound": []},
    "unlinked": [],
    "legacy_notes": [{"id": "NOTE-20261123-93", "handoffs": ["HND-20261123-82"], "attributed": True},
                     {"id": "NOTE-20261123-94", "handoffs": ["HND-20261123-80", "HND-20261123-81"], "attributed": False},
                     {"id": "NOTE-20261123-95", "handoffs": [], "attributed": False}],
    "conflicts": [],
}


class FixtureTests(Base):
    def copy_and_run(self, name):
        target = Path(self.tmp.name) / name
        shutil.copytree(FIXTURES / name, target)
        res = run_script("--records", target)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_the_embedded_fixture_folder(self):
        self.assertEqual(self.copy_and_run("embedded"), EMBEDDED_EXPECTED)

    def test_the_notes_fixture_folder(self):
        self.assertEqual(self.copy_and_run("notes"), NOTES_EXPECTED)

    def test_each_fixture_folder_holds_at_most_12_records(self):
        for name in ("embedded", "notes"):
            with self.subTest(name):
                self.assertLessEqual(len(list((FIXTURES / name).iterdir())), 12)
                self.assertGreater(len(list((FIXTURES / name).iterdir())), 0)

    def test_the_fixtures_hold_no_path_of_a_person_and_no_secret(self):
        for path in FIXTURES.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                for word in ("/Users/", "/home/", "legacy-", "token", "password", "secret", "api_key"):
                    with self.subTest(path=path.name, word=word):
                        self.assertNotIn(word, text)

    def test_the_fixtures_have_the_form_of_stored_records(self):
        for path in FIXTURES.rglob("*.md"):
            with self.subTest(path.name):
                self.assertTrue(path.read_text(encoding="utf-8").startswith("<!-- readback "))
        kinds = " ".join(p.read_text(encoding="utf-8") for p in (FIXTURES / "embedded").iterdir())
        self.assertIn(MARKER, kinds)
        self.assertIn("replaces", kinds)


if __name__ == "__main__":
    unittest.main()
