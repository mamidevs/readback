#!/usr/bin/env python3
"""Offline checks for scripts/record_preflight.py (CON-20261123-10).

Run: python3 -m unittest discover -s checks -p 'test_record_preflight.py' -v

Every request and every folder is written in a temporary folder. The fixture
texts are invented here; only the record_frame fixture is read from the repository.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

ROOT = Path(os.environ.get("PREFLIGHT_ROOT") or Path(__file__).resolve().parent.parent)
SCRIPTS = ROOT / "scripts"
SCRIPT = SCRIPTS / "record_preflight.py"
FRAME = ROOT / "checks" / "fixtures" / "record_frame"

CON = "CON-20261201-01"
CON2 = "CON-20261201-02"
HND = "HND-20261201-01"
HND2 = "HND-20261201-02"
HND3 = "HND-20261201-03"
REV = "NOTE-20261201-01"
REV2 = "NOTE-20261201-02"
ACC = "NOTE-20261201-03"
ENVELOPE = '<!-- readback project=demo-1 scope="shared" -->'

CONTRACT = (CON + "  Invented task one.\n"
            "ACC-1  The tool shall work.   Check: sh one.sh\n"
            "Out of scope: nothing else\n")
CONTRACT2 = CONTRACT.replace(CON, CON2)


def handoff(**changes):
    """A handoff in JSON form that the validator accepts for CONTRACT; None drops a key."""
    base = {"id": HND, "contract_id": CON, "replaces": "none", "from": "worker", "date": "2026-12-01",
            "state": "DONE", "readback": "task: t | limits: l | done when: ACC-1",
            "summary": "Done.", "changes": ["none"], "commit": "none",
            "checks": ["sh one.sh | exit 0 | ok"], "contract": {"ACC-1": "VERIFIED"},
            "open": "-", "issues": ["-"], "next": "Accept."}
    for key, value in changes.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = value
    return json.dumps(base, indent=2) + "\n"


def review_note(handoff_id=HND, contract_id=CON, judge="PASS | codex:x | rev1 | all hold"):
    return ("kind review\nhandoff %s\ncontract %s\nJUDGE %s\n"
            "execution codex | m | high | cli | run-1\n"
            "receipt att-1 | ends-equal | validation true | /tmp/att-1.json\n") % (handoff_id, contract_id, judge)


def acceptance_note(handoff_id=HND, contract_id=CON, review=REV, accepted="ACCEPTED yes | 2026-12-01"):
    return "kind acceptance\nhandoff %s\ncontract %s\nreview %s\n%s\n" % (handoff_id, contract_id, review, accepted)


def load_module():
    spec = importlib.util.spec_from_file_location("record_preflight_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name + "_under_test", SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write_request(self, records, name="request.json", **extra):
        path = self.dir / name
        body = dict(extra)
        body["records"] = [{"id": i, "text": t} for i, t in records]
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def write_folder(self, files, name="records"):
        folder = self.dir / name
        folder.mkdir(exist_ok=True)
        for fname, text in files.items():
            target = folder / fname
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return folder

    def run_script(self, *args, script=SCRIPT, cwd=None, env=None):
        return subprocess.run([sys.executable, str(script)] + [str(a) for a in args],
                              capture_output=True, text=True, timeout=60, cwd=cwd, env=env)

    def preflight(self, records, folder=None, **kw):
        """Run on a request of (id, text) pairs and an optional folder of {file: text}; return (proc, result)."""
        request = self.write_request(records)
        args = ["--request", request]
        if folder is not None:
            args += ["--records", self.write_folder(folder)]
        proc = self.run_script(*args, **kw)
        self.assertEqual(proc.stderr, "", proc.stderr)
        return proc, json.loads(proc.stdout)

    def entry(self, record_id, text, folder=None, extra=()):
        _, result = self.preflight([(record_id, text)] + list(extra), folder)
        return result["records"][0]


class OutputTests(Base):
    """ACC-1: the shape, the order, `ok` and the exit status."""

    def test_prints_one_json_object_with_ok_and_records(self):
        proc, result = self.preflight([(CON, CONTRACT)])
        self.assertEqual(proc.stdout.count("\n"), 1)
        self.assertTrue(proc.stdout.endswith("\n"))
        self.assertEqual(list(result), ["ok", "records"])
        self.assertIsInstance(result["ok"], bool)
        self.assertIsInstance(result["records"], list)

    def test_an_entry_holds_id_kind_checked_and_defects(self):
        entry = self.entry(CON, CONTRACT)
        for key in ("id", "kind", "checked", "defects"):
            self.assertIn(key, entry)
        self.assertEqual(entry["id"], CON)
        self.assertEqual(entry["kind"], "CON")
        self.assertIs(entry["checked"], False)
        self.assertEqual(entry["defects"], [])

    def test_one_entry_for_each_record_in_the_order_of_the_request(self):
        ids = [REV, CON, HND, "DEC-20261201-01", "free-form", ACC]
        records = [(i, "text") for i in ids]
        _, result = self.preflight(records)
        self.assertEqual([e["id"] for e in result["records"]], ids)
        _, result = self.preflight(list(reversed(records)))
        self.assertEqual([e["id"] for e in result["records"]], list(reversed(ids)))

    def test_an_empty_list_of_records_gives_ok_true_and_no_entry(self):
        proc, result = self.preflight([])
        self.assertEqual(result, {"ok": True, "records": []})
        self.assertEqual(proc.returncode, 0)

    def test_kind_is_the_prefix_of_the_id(self):
        ids = {CON: "CON", HND: "HND", REV: "NOTE", "DEC-20261201-01": "DEC",
               "LOG-20261201-01": "other", "hnd-20261201-01": "other", "CONX-1": "other", "plain": "other"}
        _, result = self.preflight([(i, "text") for i in ids])
        self.assertEqual({e["id"]: e["kind"] for e in result["records"]}, ids)

    def test_a_con_record_a_dec_record_and_a_free_form_note_are_not_checked(self):
        records = [(CON, CONTRACT), ("DEC-20261201-01", "a decision\n"), (REV, "just words, no kind line\n"),
                   ("other-1", "kind review\n")]
        proc, result = self.preflight(records)
        for entry in result["records"]:
            self.assertEqual((entry["checked"], entry["defects"]), (False, []), entry)
            self.assertEqual(set(entry), {"id", "kind", "checked", "defects"})
        self.assertEqual(proc.returncode, 0)

    def test_ok_is_true_and_the_status_is_0_without_a_defect(self):
        proc, result = self.preflight([(CON, CONTRACT), (HND, handoff())])
        self.assertEqual(result["records"][1]["defects"], [])
        self.assertIs(result["ok"], True)
        self.assertEqual(proc.returncode, 0)

    def test_ok_is_false_and_the_status_is_1_with_a_defect(self):
        proc, result = self.preflight([(CON, CONTRACT), (HND, handoff(contract_id=CON2))])
        self.assertTrue(result["records"][1]["defects"])
        self.assertIs(result["ok"], False)
        self.assertEqual(proc.returncode, 1)

    def test_one_defect_in_a_later_entry_is_enough_for_ok_false(self):
        proc, result = self.preflight([(CON, CONTRACT), (HND, handoff()), (HND2, handoff(id=HND2, replaces=HND3))])
        self.assertEqual(result["records"][0]["defects"], [])
        self.assertEqual(result["records"][1]["defects"], [])
        self.assertEqual(result["records"][2]["defects"], ["replaces-missing: " + HND3])
        self.assertIs(result["ok"], False)
        self.assertEqual(proc.returncode, 1)

    def test_a_request_without_records_option_searches_only_the_request(self):
        proc = self.run_script("--request", self.write_request([(HND, handoff())]))
        result = json.loads(proc.stdout)
        self.assertEqual(result["records"][0]["validation"], "contract-missing")
        self.assertEqual(proc.returncode, 1)

    def test_other_keys_of_the_request_and_of_a_record_are_ignored(self):
        path = self.dir / "request.json"
        path.write_text(json.dumps({"operation_id": 5, "log_lines": 7, "records": [
            {"id": CON, "text": CONTRACT, "scope": "nowhere", "more": [1]}]}), encoding="utf-8")
        proc = self.run_script("--request", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["records"][0]["id"], CON)

    def test_two_runs_give_the_same_bytes(self):
        request = self.write_request([(CON, CONTRACT), (HND, handoff()), (REV, review_note()), (ACC, acceptance_note())])
        folder = self.write_folder({HND2 + ".md": ENVELOPE + "\n" + handoff(id=HND2)})
        outputs = [self.run_script("--request", request, "--records", folder) for _ in range(2)]
        self.assertEqual(outputs[0].stdout, outputs[1].stdout)
        self.assertEqual(outputs[0].returncode, outputs[1].returncode)
        self.assertNotEqual(outputs[0].stdout, "")

    def test_no_file_is_written_and_no_file_changes(self):
        scripts_copy = self.dir / "scripts_copy"
        shutil.copytree(SCRIPTS, scripts_copy, ignore=shutil.ignore_patterns("__pycache__"))
        request = self.write_request([(CON, CONTRACT), (HND, handoff()), (REV, review_note())])
        folder = self.write_folder({HND2 + ".md": ENVELOPE + "\n" + handoff(id=HND2)})
        cwd = self.dir / "cwd"
        cwd.mkdir()

        def snapshot():
            return {str(p.relative_to(self.dir)): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
                    for p in sorted(self.dir.rglob("*")) if p.is_file()}

        before = snapshot()
        proc = self.run_script("--request", request, "--records", folder, script=scripts_copy / "record_preflight.py",
                               cwd=cwd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(snapshot(), before)
        self.assertEqual(list(cwd.iterdir()), [])
        self.assertFalse(any(p.suffix == ".pyc" or p.name == "__pycache__" for p in self.dir.rglob("*")))

    def test_the_script_runs_as_a_command_from_another_folder(self):
        request = self.write_request([(CON, CONTRACT)])
        cwd = self.dir / "elsewhere"
        cwd.mkdir()
        proc = subprocess.run([sys.executable, str(SCRIPT), "--request", str(request)], cwd=cwd,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"ok": True, "records": [
            {"id": CON, "kind": "CON", "checked": False, "defects": []}]})
        self.assertTrue(SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n"))


class ErrorTests(Base):
    """ACC-4: every exit-2 case prints nothing on standard output and one line on standard error."""

    def refused(self, *args, **kw):
        proc = self.run_script(*args, **kw)
        self.assertEqual(proc.returncode, 2, (proc.stdout, proc.stderr))
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.endswith("\n"), repr(proc.stderr))
        self.assertEqual(proc.stderr.count("\n"), 1, repr(proc.stderr))
        self.assertTrue(proc.stderr.strip())
        return proc.stderr

    def request_text(self, text, raw=None):
        path = self.dir / "request.json"
        if raw is not None:
            path.write_bytes(raw)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def test_a_request_file_that_is_missing(self):
        self.refused("--request", self.dir / "nothing.json")

    def test_a_request_that_is_a_folder(self):
        self.refused("--request", self.dir)

    def test_a_request_that_is_not_utf8(self):
        self.refused("--request", self.request_text(None, raw=b'{"records": [\xff]}'))

    def test_a_request_that_is_not_json(self):
        self.refused("--request", self.request_text("{records: ["))
        self.refused("--request", self.request_text(""))

    def test_a_request_that_is_no_object(self):
        for text in ("[]", '"records"', "7", "null", '[{"id": "a", "text": "b"}]'):
            with self.subTest(text=text):
                self.refused("--request", self.request_text(text))

    def test_a_request_without_a_list_records(self):
        for text in ("{}", '{"records": null}', '{"records": {"id": "a", "text": "b"}}', '{"records": "x"}',
                     '{"record": []}'):
            with self.subTest(text=text):
                self.refused("--request", self.request_text(text))

    def test_a_record_that_is_no_object(self):
        for item in ("7", '"a"', "null", "[]", '["id", "text"]'):
            with self.subTest(item=item):
                self.refused("--request", self.request_text('{"records": [%s]}' % item))

    def test_a_record_without_the_texts_id_and_text(self):
        for item in ('{"id": "a"}', '{"text": "b"}', '{"id": 1, "text": "b"}', '{"id": "a", "text": 2}',
                     '{"id": null, "text": "b"}', '{"id": "a", "text": null}', '{"id": ["a"], "text": "b"}',
                     '{"id": "a", "text": {"x": 1}}'):
            with self.subTest(item=item):
                self.refused("--request", self.request_text('{"records": [%s]}' % item))

    def test_one_bad_record_among_good_ones_refuses_the_whole_request(self):
        good = '{"id": "%s", "text": "x"}' % CON
        self.refused("--request", self.request_text('{"records": [%s, {"id": "b"}, %s]}' % (good, good)))

    def test_a_folder_that_is_given_and_is_missing(self):
        request = self.write_request([(CON, CONTRACT)])
        self.refused("--request", request, "--records", self.dir / "no-such-folder")

    def test_a_folder_that_is_a_file(self):
        request = self.write_request([(CON, CONTRACT)])
        self.refused("--request", request, "--records", request)

    def test_a_missing_folder_is_refused_even_when_the_request_needs_no_folder(self):
        request = self.write_request([])
        self.refused("--request", request, "--records", self.dir / "gone")

    def test_a_malformed_argument(self):
        request = self.write_request([(CON, CONTRACT)])
        folder = self.write_folder({})
        cases = [
            [],
            ["--records", folder],
            ["--request"],
            ["--request", request, "--records"],
            ["--request", request, "--unknown", "x"],
            ["--request", request, "extra"],
            ["extra"],
            ["--request", request, "--request", request],
            ["--request", request, "--records", folder, "--records", folder],
            ["--request=" + str(request)],
            ["-r", str(request)],
            ["--req", str(request)],
            ["--request", request, "--help"],
        ]
        for args in cases:
            with self.subTest(args=[str(a) for a in args]):
                self.refused(*args)

    def test_the_reason_stands_on_one_line_even_when_the_path_holds_a_line_break(self):
        self.refused("--request", self.dir / "no\nsuch\nfile.json")

    def copy_scripts(self, drop=(), replace=None):
        copy = self.dir / "scripts_copy"
        shutil.copytree(SCRIPTS, copy, ignore=shutil.ignore_patterns("__pycache__"))
        for name in drop:
            (copy / name).unlink()
        for name, text in (replace or {}).items():
            (copy / name).write_text(text, encoding="utf-8")
        return copy / "record_preflight.py"

    def test_a_helper_that_is_absent_gives_a_line_that_starts_with_unavailable(self):
        request = self.write_request([(CON, CONTRACT)])
        for name in ("validate_handoff.py", "closure_note.py"):
            with self.subTest(name=name):
                script = self.copy_scripts(drop=[name])
                proc = self.run_script("--request", request, script=script)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertTrue(proc.stderr.startswith("unavailable:"), proc.stderr)
                self.assertEqual(proc.stderr.count("\n"), 1)
                self.assertIn(name, proc.stderr)
                shutil.rmtree(script.parent)

    def test_a_helper_that_cannot_be_loaded_gives_the_same_line(self):
        request = self.write_request([(CON, CONTRACT)])
        for name in ("validate_handoff.py", "closure_note.py"):
            with self.subTest(name=name):
                script = self.copy_scripts(replace={name: "def broken(:\n"})
                proc = self.run_script("--request", request, script=script)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertTrue(proc.stderr.startswith("unavailable:"), proc.stderr)
                self.assertEqual(proc.stderr.count("\n"), 1)
                shutil.rmtree(script.parent)

    def test_the_helpers_come_from_the_folder_of_the_script_not_from_the_path(self):
        request = self.write_request([(CON, CONTRACT)])
        script = self.copy_scripts(drop=["validate_handoff.py"])
        env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
        proc = self.run_script("--request", request, script=script, env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(proc.stderr.startswith("unavailable:"), proc.stderr)

    def test_a_refusal_for_arguments_comes_before_the_load_of_helpers(self):
        script = self.copy_scripts(drop=["validate_handoff.py"])
        proc = self.run_script(script=script)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(proc.stderr.startswith("unavailable:"), proc.stderr)

    def test_the_helpers_stay_unavailable_even_for_a_request_that_needs_them_not(self):
        script = self.copy_scripts(drop=["closure_note.py"])
        proc = self.run_script("--request", self.write_request([]), script=script)
        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assertTrue(proc.stderr.startswith("unavailable:"))


class HandoffTests(Base):
    """ACC-2: `validation` and `replaces` of an HND entry, and the defects of the handoff rules."""

    def hnd(self, text, folder=None, extra=()):
        return self.entry(HND, text, folder, extra=[(CON, CONTRACT)] + list(extra))

    def test_an_hnd_entry_holds_validation_and_replaces_and_is_checked(self):
        entry = self.hnd(handoff())
        self.assertEqual(list(entry), ["id", "kind", "checked", "validation", "replaces", "defects"])
        self.assertEqual((entry["kind"], entry["checked"]), ("HND", True))

    def test_validation_true_for_the_contract_in_the_request(self):
        entry = self.hnd(handoff())
        self.assertEqual((entry["validation"], entry["defects"]), ("true", []))

    def test_validation_true_for_the_contract_in_the_folder_with_its_envelope_line(self):
        _, result = self.preflight([(HND, handoff())], {CON + ".md": ENVELOPE + "\n" + CONTRACT})
        entry = result["records"][0]
        self.assertEqual((entry["validation"], entry["defects"]), ("true", []))
        self.assertIs(result["ok"], True)

    def test_contract_missing_when_the_lookup_finds_no_contract(self):
        entry = self.entry(HND, handoff())
        self.assertEqual(entry["validation"], "contract-missing")
        self.assertEqual(entry["defects"], ["contract-missing: " + CON])
        entry = self.entry(HND, handoff(), folder={"other.md": "x\n", CON2 + ".md": CONTRACT2})
        self.assertEqual(entry["defects"], ["contract-missing: " + CON])

    def test_unbound_when_the_handoff_holds_no_contract_id(self):
        entry = self.hnd(handoff(contract_id=None))
        self.assertEqual((entry["validation"], entry["defects"]), ("unbound", []))

    def test_unbound_means_the_validator_did_not_run(self):
        entry = self.hnd(handoff(contract_id=None, checks=[], contract={}))
        self.assertEqual((entry["validation"], entry["defects"]), ("unbound", []))

    def test_validation_false_n_with_the_number_of_defect_level_findings(self):
        bad = handoff(contract={})
        findings = load_helper("validate_handoff").validate(CONTRACT, bad)["findings"]
        defects = [f for f in findings if f["level"] == "defect"]
        entry = self.hnd(bad)
        self.assertEqual(len(defects), 2)
        self.assertEqual(entry["validation"], "false 2")
        self.assertEqual(entry["defects"], ["validation: false 2"])

    def test_n_counts_the_defect_level_and_leaves_the_unresolved_findings_out(self):
        bad = handoff(contract={"ACC-1": "FAILED"}, checks=["sh other.sh | exit 0 | ok"])
        findings = load_helper("validate_handoff").validate(CONTRACT, bad)["findings"]
        self.assertEqual(sorted(f["level"] for f in findings), ["defect", "unresolved"])
        entry = self.hnd(bad)
        self.assertEqual((entry["validation"], entry["defects"]), ("false 1", ["validation: false 1"]))

    def test_unparsed_is_no_defect(self):
        entry = self.hnd(handoff(checks=["sh other.sh | exit 0 | ok"]))
        self.assertEqual((entry["validation"], entry["defects"]), ("unparsed", []))

    def test_unreadable_for_a_handoff_that_is_broken_json(self):
        entry = self.hnd("{ \"id\": \"%s\", broken\n" % HND)
        self.assertEqual(entry["validation"], "unreadable")
        self.assertEqual(entry["defects"], ["validation: unreadable"])
        self.assertEqual(entry["replaces"], "absent")

    def test_unreadable_reads_no_link_key(self):
        text = '{"contract_id": "%s", "replaces": "%s", broken' % (CON2, HND3)
        entry = self.hnd(text)
        self.assertEqual((entry["validation"], entry["replaces"]), ("unreadable", "absent"))
        self.assertEqual(entry["defects"], ["validation: unreadable"])

    def test_unreadable_for_a_text_without_any_key(self):
        entry = self.hnd("\n\n")
        self.assertEqual((entry["validation"], entry["defects"]), ("unreadable", ["validation: unreadable"]))

    def test_replaces_absent_when_the_handoff_holds_no_such_key(self):
        entry = self.hnd(handoff(replaces=None))
        self.assertEqual((entry["replaces"], entry["defects"]), ("absent", []))

    def test_replaces_none(self):
        entry = self.hnd(handoff(replaces="none"))
        self.assertEqual((entry["replaces"], entry["defects"]), ("none", []))

    def test_replaces_found_in_the_request(self):
        entry = self.hnd(handoff(replaces=HND2), extra=[(HND2, handoff(id=HND2))])
        self.assertEqual((entry["replaces"], entry["defects"]), ("found", []))

    def test_replaces_found_in_the_folder(self):
        entry = self.hnd(handoff(replaces=HND2), folder={HND2 + ".md": ENVELOPE + "\n" + handoff(id=HND2)})
        self.assertEqual((entry["replaces"], entry["defects"]), ("found", []))

    def test_replaces_missing_gives_a_defect(self):
        entry = self.hnd(handoff(replaces=HND2))
        self.assertEqual(entry["replaces"], "missing")
        self.assertEqual(entry["defects"], ["replaces-missing: " + HND2])

    def test_replaces_is_read_when_the_handoff_is_unbound(self):
        entry = self.hnd(handoff(contract_id=None, replaces=HND2))
        self.assertEqual((entry["validation"], entry["replaces"]), ("unbound", "missing"))
        self.assertEqual(entry["defects"], ["replaces-missing: " + HND2])

    def test_replaces_is_read_when_the_contract_is_missing(self):
        entry = self.entry(HND, handoff(replaces=HND2))
        self.assertEqual((entry["validation"], entry["replaces"]), ("contract-missing", "missing"))

    def test_the_link_keys_are_read_without_spaces_tabs_and_line_breaks_at_both_ends(self):
        text = handoff(contract_id=" \t" + CON + "\n ", replaces="\n\t " + HND2 + " \t")
        entry = self.hnd(text, extra=[(HND2, handoff(id=HND2))])
        self.assertEqual((entry["validation"], entry["replaces"], entry["defects"]), ("true", "found", []))

    def test_the_link_keys_are_read_in_the_text_form_too(self):
        text = "id %s\ncontract_id %s\nreplaces %s\n" % (HND, CON, HND2)
        entry = self.hnd(text)
        self.assertEqual(entry["replaces"], "missing")
        self.assertNotEqual(entry["validation"], "contract-missing")
        self.assertNotEqual(entry["validation"], "unbound")

    def test_an_empty_link_value_is_looked_up_like_any_other(self):
        entry = self.hnd(handoff(contract_id=""))
        self.assertEqual((entry["validation"], entry["defects"]), ("contract-missing", ["contract-missing: "]))

    def test_the_defects_stand_in_the_order_of_the_rules(self):
        entry = self.entry(HND, handoff(replaces=HND2))
        self.assertEqual(entry["defects"], ["contract-missing: " + CON, "replaces-missing: " + HND2])
        entry = self.hnd(handoff(contract={}, replaces=HND2))
        self.assertEqual(entry["defects"], ["validation: false 2", "replaces-missing: " + HND2])

    def test_the_contract_that_the_id_names_is_the_one_that_is_validated(self):
        _, result = self.preflight([(CON, CONTRACT2), (HND, handoff())])
        self.assertEqual(result["records"][1]["validation"], "false 1")

    def test_a_handoff_with_the_lead_marker_is_read_up_to_the_marker(self):
        text = handoff() + "<!-- readback lead -->\nJUDGE PASS | x | y | z\nreplaces-not-a-key\n"
        entry = self.hnd(text)
        self.assertEqual((entry["validation"], entry["defects"]), ("true", []))

    def test_a_handoff_in_the_request_with_an_envelope_line_is_read(self):
        entry = self.hnd(ENVELOPE + "\n" + handoff())
        self.assertEqual((entry["validation"], entry["defects"]), ("true", []))


class LookupTests(Base):
    """The Definitions: folder, lookup and the removal of the envelope line."""

    def valid_with(self, contract_id=CON, folder=None, request=()):
        records = list(request) + [(HND, handoff(contract_id=contract_id))]
        _, result = self.preflight(records, folder)
        return result["records"][-1]

    def test_the_request_is_searched_before_the_folder(self):
        entry = self.valid_with(request=[(CON, CONTRACT)], folder={CON + ".md": CONTRACT2})
        self.assertEqual(entry["validation"], "true")

    def test_the_folder_is_not_searched_first(self):
        entry = self.valid_with(request=[(CON, CONTRACT2)], folder={CON + ".md": CONTRACT})
        self.assertEqual(entry["validation"], "false 1")

    def test_the_folder_is_searched_when_the_request_lacks_the_id(self):
        entry = self.valid_with(request=[(CON2, CONTRACT2)], folder={CON + ".md": CONTRACT})
        self.assertEqual(entry["validation"], "true")

    def test_a_file_in_a_subfolder_is_not_in_the_folder(self):
        entry = self.valid_with(folder={"sub/" + CON + ".md": CONTRACT})
        self.assertEqual(entry["validation"], "contract-missing")

    def test_an_id_with_a_path_separator_names_no_file(self):
        folder = {"sub/" + CON + ".md": CONTRACT}
        for value in ("sub/" + CON, "./" + CON, "sub\\" + CON):
            with self.subTest(value=value):
                entry = self.valid_with(contract_id=value, folder=folder)
                self.assertEqual(entry["validation"], "contract-missing")

    def test_an_id_cannot_leave_the_folder(self):
        outside = self.dir / (CON + ".md")
        outside.write_text(CONTRACT, encoding="utf-8")
        entry = self.valid_with(contract_id="../" + CON, folder={"keep.md": "x\n"})
        self.assertEqual(entry["validation"], "contract-missing")
        entry = self.valid_with(contract_id=str(outside)[:-3], folder={"keep.md": "x\n"})
        self.assertEqual(entry["validation"], "contract-missing")

    def test_only_a_file_named_id_dot_md_is_a_record(self):
        for name in (CON + ".txt", CON, CON + ".md.bak", "x" + CON + ".md"):
            with self.subTest(name=name):
                entry = self.valid_with(folder={name: CONTRACT})
                self.assertEqual(entry["validation"], "contract-missing")

    def test_a_folder_that_is_empty_finds_nothing(self):
        entry = self.valid_with(folder={})
        self.assertEqual(entry["validation"], "contract-missing")

    def test_a_folder_record_that_is_no_text_is_not_found(self):
        folder = self.write_folder({})
        (folder / (CON + ".md")).write_bytes(b"\xff\xfe\x00")
        request = self.write_request([(HND, handoff())])
        proc = self.run_script("--request", request, "--records", folder)
        self.assertEqual(json.loads(proc.stdout)["records"][0]["validation"], "contract-missing")

    def test_a_request_record_is_found_by_its_whole_id_only(self):
        _, result = self.preflight([(CON + "x", CONTRACT), (HND, handoff())])
        self.assertEqual(result["records"][1]["validation"], "contract-missing")

    def test_the_envelope_line_of_a_folder_record_is_removed(self):
        module = load_module()
        body = "line one\nline two\n"
        self.assertEqual(module.unframe(ENVELOPE + "\n" + body), body)
        self.assertEqual(module.unframe("\n \n" + ENVELOPE + " \t\r\n" + body), body)

    def test_only_the_first_line_that_is_not_blank_can_be_the_envelope(self):
        module = load_module()
        text = "first\n" + ENVELOPE + "\nlast\n"
        self.assertEqual(module.unframe(text), text)
        text = ENVELOPE + "\n" + ENVELOPE + "\nlast\n"
        self.assertEqual(module.unframe(text), ENVELOPE + "\nlast\n")

    def test_the_lead_marker_and_an_unfinished_line_are_no_envelope(self):
        module = load_module()
        for text in ("<!-- readback lead -->\nbody\n", "<!-- readback x\nbody\n", "<!-- other -->\nbody\n", "body\n", ""):
            with self.subTest(text=text):
                self.assertEqual(module.unframe(text), text)

    def test_a_folder_record_is_used_without_its_envelope_line(self):
        module = load_module()
        folder = self.write_folder({CON + ".md": ENVELOPE + "\n" + CONTRACT})
        found = module.Lookup([], str(folder)).find(CON)
        self.assertEqual(found, CONTRACT)
        self.assertIsNone(module.Lookup([], str(folder)).find(CON2))
        self.assertIsNone(module.Lookup([], None).find(CON))


class NoteTests(Base):
    """ACC-3: `check` and `links` of a review or acceptance note, and the defects of the note rules."""

    def note(self, record_id, text, folder=None, extra=()):
        return self.entry(record_id, text, folder, extra=list(extra))

    def stock(self):
        """The records that a good note links to."""
        return [(CON, CONTRACT), (HND, handoff()), (REV, review_note())]

    def links(self, entry):
        return [(l["key"], l["value"], l["state"]) for l in entry["links"]]

    def test_a_review_note_with_check_true(self):
        entry = self.note(REV, review_note(), extra=[(CON, CONTRACT), (HND, handoff())])
        self.assertEqual(list(entry), ["id", "kind", "checked", "check", "links", "defects"])
        self.assertEqual((entry["kind"], entry["checked"], entry["check"]), ("NOTE", True, True))
        self.assertEqual(entry["defects"], [])
        self.assertEqual(self.links(entry), [("handoff", HND, "found"), ("contract", CON, "found")])

    def test_an_acceptance_note_with_check_true(self):
        entry = self.note(ACC, acceptance_note(), extra=self.stock())
        self.assertEqual((entry["checked"], entry["check"], entry["defects"]), (True, True, []))
        self.assertEqual(self.links(entry), [("handoff", HND, "found"), ("contract", CON, "found"),
                                             ("review", REV, "found")])

    def test_a_review_note_with_check_false_has_one_defect_for_each_finding(self):
        text = review_note(judge="MAYBE | codex:x")
        findings = load_helper("closure_note").check_note(text)["findings"]
        self.assertEqual(len(findings), 2)
        entry = self.note(REV, text, extra=[(CON, CONTRACT), (HND, handoff())])
        self.assertEqual((entry["checked"], entry["check"]), (True, False))
        self.assertEqual(entry["defects"], ["check: " + f for f in findings])

    def test_an_acceptance_note_with_check_false_has_one_defect_for_each_finding(self):
        text = acceptance_note(accepted="ACCEPTED maybe | 2026-13-45")
        findings = load_helper("closure_note").check_note(text)["findings"]
        self.assertEqual(len(findings), 2)
        entry = self.note(ACC, text, extra=self.stock())
        self.assertEqual((entry["checked"], entry["check"]), (True, False))
        self.assertEqual(entry["defects"], ["check: " + f for f in findings])

    def test_a_note_with_a_missing_key_has_check_false(self):
        text = "kind review\nhandoff %s\ncontract %s\n" % (HND, CON)
        entry = self.note(REV, text, extra=[(CON, CONTRACT), (HND, handoff())])
        self.assertEqual((entry["checked"], entry["check"]), (True, False))
        self.assertEqual(entry["defects"], ["check: missing key JUDGE", "check: missing key execution",
                                            "check: missing key receipt"])

    def test_a_note_with_an_envelope_line_is_checked(self):
        entry = self.note(REV, ENVELOPE + "\n" + review_note(), extra=[(CON, CONTRACT), (HND, handoff())])
        self.assertEqual((entry["check"], entry["defects"]), (True, []))

    def test_the_link_state_found_and_missing_for_each_of_the_three_keys(self):
        keys = {"handoff": (HND, "handoff"), "contract": (CON, "contract"), "review": (REV, "review")}
        records = {"handoff": (HND, handoff()), "contract": (CON, CONTRACT), "review": (REV, review_note())}
        for key, (value, _) in keys.items():
            for present in (True, False):
                with self.subTest(key=key, present=present):
                    extra = [r for k, r in records.items() if k != key or present]
                    entry = self.note(ACC, acceptance_note(), extra=extra)
                    state = {k: s for k, _, s in self.links(entry)}
                    self.assertEqual(state[key], "found" if present else "missing")
                    expected = [] if present else ["link-missing: %s %s" % (key, value)]
                    self.assertEqual([d for d in entry["defects"] if d.startswith("link-missing")], expected)

    def test_a_link_is_found_in_the_folder_with_its_envelope_line(self):
        folder = {HND + ".md": ENVELOPE + "\n" + handoff(), CON + ".md": ENVELOPE + "\n" + CONTRACT,
                  REV + ".md": ENVELOPE + "\n" + review_note()}
        entry = self.note(ACC, acceptance_note(), folder=folder)
        self.assertEqual([s for _, _, s in self.links(entry)], ["found"] * 3)
        self.assertEqual(entry["defects"], [])

    def test_a_link_with_the_value_none_gives_no_entry_and_no_defect(self):
        entry = self.note(ACC, acceptance_note(review="none"), extra=self.stock()[:2])
        self.assertEqual([k for k, _, _ in self.links(entry)], ["handoff", "contract"])
        self.assertEqual(entry["defects"], [])
        entry = self.note(ACC, acceptance_note(handoff_id="none"), extra=self.stock())
        self.assertEqual([k for k, _, _ in self.links(entry)], ["contract", "review"])
        self.assertEqual(entry["defects"], ["check: handoff value is no HND id: 'none'", "review-mismatch: " + REV])

    def test_links_stand_in_the_order_handoff_contract_review(self):
        text = "kind acceptance\nreview %s\ncontract %s\nhandoff %s\nACCEPTED yes | 2026-12-01\n" % (REV, CON, HND)
        entry = self.note(ACC, text, extra=self.stock())
        self.assertEqual([k for k, _, _ in self.links(entry)], ["handoff", "contract", "review"])

    def test_a_key_that_the_note_does_not_hold_gives_no_link(self):
        text = "kind review\nhandoff %s\n" % HND
        entry = self.note(REV, text, extra=[(HND, handoff())])
        self.assertEqual(self.links(entry), [("handoff", HND, "found")])

    def test_a_link_value_is_read_without_spaces_at_both_ends(self):
        text = acceptance_note().replace("handoff " + HND, "handoff \t" + HND + "  \t")
        entry = self.note(ACC, text, extra=self.stock())
        self.assertEqual(self.links(entry)[0], ("handoff", HND, "found"))

    def test_the_defects_stand_in_the_order_check_link_contract_review(self):
        text = acceptance_note(contract_id=CON2, accepted="ACCEPTED maybe | 2026-12-01")
        other = review_note(handoff_id=HND2)
        entry = self.note(ACC, text, extra=[(HND, handoff()), (REV, other)])
        names = [d.split(":")[0] for d in entry["defects"]]
        self.assertEqual(names, ["check", "link-missing", "contract-mismatch", "review-mismatch"])
        self.assertEqual(entry["defects"][1:], ["link-missing: contract " + CON2,
                                                "contract-mismatch: %s %s" % (CON2, CON),
                                                "review-mismatch: " + REV])

    def test_contract_mismatch_when_the_found_handoff_names_another_contract(self):
        text = acceptance_note(contract_id=CON2)
        extra = [(CON, CONTRACT), (CON2, CONTRACT2), (HND, handoff()), (REV, review_note(contract_id=CON2))]
        entry = self.note(ACC, text, extra=extra)
        self.assertEqual(entry["defects"], ["contract-mismatch: %s %s" % (CON2, CON)])
        entry = self.note(REV2, review_note(contract_id=CON2), extra=extra)
        self.assertEqual(entry["defects"], ["contract-mismatch: %s %s" % (CON2, CON)])

    def test_no_contract_mismatch_when_the_contracts_agree(self):
        entry = self.note(ACC, acceptance_note(), extra=self.stock())
        self.assertEqual(entry["defects"], [])

    def test_no_contract_mismatch_when_the_handoff_is_missing(self):
        entry = self.note(REV, review_note(contract_id=CON2), extra=[(CON, CONTRACT), (CON2, CONTRACT2)])
        self.assertEqual(entry["defects"], ["link-missing: handoff " + HND])

    def test_no_contract_mismatch_when_the_handoff_holds_no_contract_id(self):
        entry = self.note(REV, review_note(contract_id=CON2),
                          extra=[(CON2, CONTRACT2), (HND, handoff(contract_id=None))])
        self.assertEqual(entry["defects"], [])

    def test_no_contract_mismatch_when_the_handoff_is_unreadable(self):
        entry = self.note(REV, review_note(contract_id=CON2), extra=[(CON2, CONTRACT2), (HND, "{ broken")])
        self.assertEqual(entry["defects"], [])

    def test_contract_mismatch_reads_the_contract_id_without_spaces_at_both_ends(self):
        extra = [(CON, CONTRACT), (HND, handoff(contract_id="  " + CON + "\t\n"))]
        entry = self.note(REV, review_note(), extra=extra)
        self.assertEqual(entry["defects"], [])

    def test_review_mismatch_when_the_review_note_names_another_handoff(self):
        extra = self.stock()[:2] + [(HND2, handoff(id=HND2)), (REV, review_note(handoff_id=HND2))]
        entry = self.note(ACC, acceptance_note(), extra=extra)
        self.assertEqual(entry["defects"], ["review-mismatch: " + REV])

    def test_review_mismatch_when_the_review_note_is_no_valid_review_note(self):
        extra = self.stock()[:2]
        cases = {"check false": review_note(judge="MAYBE | x"),
                 "an acceptance note": acceptance_note(),
                 "another kind": review_note().replace("kind review", "kind memo"),
                 "no kind": "words only\n"}
        for name, text in cases.items():
            with self.subTest(name):
                entry = self.note(ACC, acceptance_note(), extra=extra + [(REV, text)])
                self.assertEqual(entry["defects"], ["review-mismatch: " + REV])

    def test_no_review_mismatch_when_the_review_note_is_valid_and_names_the_same_handoff(self):
        entry = self.note(ACC, acceptance_note(), extra=self.stock())
        self.assertEqual(entry["defects"], [])

    def test_no_review_mismatch_when_the_review_note_is_missing(self):
        entry = self.note(ACC, acceptance_note(), extra=self.stock()[:2])
        self.assertEqual(entry["defects"], ["link-missing: review " + REV])

    def test_no_review_mismatch_for_a_review_note_that_names_a_review(self):
        text = review_note() + "review %s\n" % REV2
        entry = self.note(REV, text, extra=[(CON, CONTRACT), (HND, handoff()), (REV2, "words\n")])
        self.assertEqual(entry["defects"], [])

    def test_the_review_note_is_read_from_the_folder_too(self):
        folder = {REV + ".md": ENVELOPE + "\n" + review_note(handoff_id=HND2)}
        entry = self.note(ACC, acceptance_note(), extra=self.stock()[:2], folder=folder)
        self.assertEqual(entry["defects"], ["review-mismatch: " + REV])

    def test_a_note_with_another_kind_line_is_not_checked(self):
        for kind in ("memo", "Review", "reviewed", "review acceptance", "", "withdrawal"):
            with self.subTest(kind=kind):
                text = "kind %s\nhandoff %s\ncontract %s\n" % (kind, HND2, CON2)
                entry = self.note(REV, text)
                self.assertEqual(entry, {"id": REV, "kind": "NOTE", "checked": False, "defects": []})

    def test_a_note_whose_first_kind_line_is_another_word_is_not_checked(self):
        text = "kind memo\n" + review_note()
        entry = self.note(REV, text)
        self.assertEqual((entry["checked"], entry["defects"]), (False, []))

    def test_the_kind_value_is_read_without_spaces_at_both_ends(self):
        text = review_note().replace("kind review", "kind \t review  ")
        entry = self.note(REV, text, extra=[(CON, CONTRACT), (HND, handoff())])
        self.assertEqual((entry["checked"], entry["check"]), (True, True))

    def test_a_free_form_note_and_an_empty_note_are_not_checked(self):
        for text in ("Free words about the work.\n", "\n", "kindly review\n"):
            with self.subTest(text=text):
                entry = self.note(REV, text)
                self.assertEqual(entry, {"id": REV, "kind": "NOTE", "checked": False, "defects": []})

    def test_the_result_does_not_depend_on_the_words_check_note_accepts(self):
        module = load_module()
        self.assertEqual(module.CHECKED_KINDS, ("review", "acceptance"))


class ExampleTests(Base):
    """A fixture handoff of the repository and a handoff made with scripts/handoff_skeleton.py."""

    def test_the_record_frame_fixture_handoff_and_its_contract(self):
        handoff_text = (FRAME / "handoff.json").read_text(encoding="utf-8")
        contract_text = (FRAME / "contract.txt").read_text(encoding="utf-8")
        _, result = self.preflight([("CON-20261117-01", contract_text), ("HND-20261117-01", handoff_text)])
        self.assertEqual(result["records"][0], {"id": "CON-20261117-01", "kind": "CON", "checked": False, "defects": []})
        self.assertEqual(result["records"][1], {"id": "HND-20261117-01", "kind": "HND", "checked": True,
                                                "validation": "unbound", "replaces": "absent", "defects": []})
        self.assertIs(result["ok"], True)

    def skeleton(self):
        contract = self.dir / "contract.txt"
        contract.write_text(CON + "  Invented task two.\n"
                            "ACC-1  The tool shall work.   Check: sh one.sh\n"
                            "ACC-2  The tool shall pass.   Check: sh two.sh\n"
                            "Out of scope: nothing else\n", encoding="utf-8")
        proc = subprocess.run([sys.executable, str(SCRIPTS / "handoff_skeleton.py"), str(contract)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return contract.read_text(encoding="utf-8"), proc.stdout

    def test_a_handoff_made_with_the_skeleton_and_filled(self):
        contract, text = self.skeleton()
        data = json.loads(text)
        self.assertEqual(data["contract_id"], CON)
        data.update({"id": HND, "replaces": "none", "from": "worker", "date": "2026-12-01", "state": "DONE",
                     "readback": "task: t | limits: l | done when: ACC-1, ACC-2", "summary": "Done.",
                     "changes": ["none"], "commit": "none", "open": "-", "issues": ["-"], "next": "Accept.",
                     "checks": ["sh one.sh | exit 0 | ok", "sh two.sh | exit 0 | ok"],
                     "contract": {"ACC-1": "VERIFIED", "ACC-2": "VERIFIED"}})
        proc, result = self.preflight([(CON, contract), (HND, json.dumps(data, indent=2) + "\n")])
        self.assertEqual(result["records"][1]["validation"], "true")
        self.assertEqual(result["records"][1]["replaces"], "none")
        self.assertEqual(result["records"][1]["defects"], [])
        self.assertEqual((result["ok"], proc.returncode), (True, 0))

    def test_a_skeleton_that_is_not_filled_has_a_defect(self):
        contract, text = self.skeleton()
        proc, result = self.preflight([(CON, contract), (HND, text)])
        entry = result["records"][1]
        self.assertTrue(entry["validation"].startswith("false "), entry)
        self.assertEqual(entry["defects"], ["validation: " + entry["validation"], "replaces-missing: <HND id or none>"])
        self.assertEqual((result["ok"], proc.returncode), (False, 1))


if __name__ == "__main__":
    unittest.main()
