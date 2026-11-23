#!/usr/bin/env python3
"""Offline checks for the links of a handoff (CON-20261123-07).

A handoff may hold `contract_id` and `replaces` directly after `id`. The skeleton prints them
and `validate` checks them as rule V7.
Run: python3 -m unittest discover -s checks -p 'test_handoff_links.py' -v
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIX = ROOT / "checks" / "fixtures"
MAIN_SHA = "9a6c271"
sys.path.insert(0, str(SCRIPTS))
sys.dont_write_bytecode = True
import validate_handoff as vh  # noqa: E402

PLACEHOLDER = "<HND id or none>"
RF_CONTRACT = (FIX / "record_frame" / "contract.txt").read_text()
RF_JSON = (FIX / "record_frame" / "handoff.json").read_text()
RF_SPACED = (FIX / "record_frame" / "handoff.txt").read_text()
RF_COLON = (FIX / "record_frame" / "handoff-colon.txt").read_text()
RF_LEAD = (FIX / "record_frame" / "lead-lines.txt").read_text()
CON = "CON-20261117-01"
OWN = "HND-20261117-01"
ENVELOPE = '<!-- readback project=demo-00000000 scope="shared" -->\n'

SKEL_CONTRACT = ("CON-20261123-90  Do a thing.\n"
                 "ACC-1  The tool shall do 1.  Check: sh a.sh\n"
                 "ACC-2  The tool shall do 2.  Check: sh b.sh\n"
                 "Out of scope: nothing\n")


def v7(result):
    return [f for f in result["findings"] if f["rule"] == "V7"]


def with_links_json(contract_id=CON, replaces="none", extra=""):
    """The fixture JSON handoff with the two keys after `id`. `extra` is raw JSON text
    (member lines) inserted after them."""
    head, rest = RF_JSON.split('  "from"', 1)
    lines = ""
    if contract_id is not None:
        lines += '  "contract_id": %s,\n' % json.dumps(contract_id)
    if replaces is not None:
        lines += '  "replaces": %s,\n' % json.dumps(replaces)
    return head + lines + extra + '  "from"' + rest


def with_links_text(body, sep, contract_id=CON, replaces="none", extra=""):
    """A text fixture with the two keys after the `id` line. `sep` is the text between key
    and value ("" is not used: pass " " or ": ")."""
    first, rest = body.split("\n", 1)
    lines = ""
    if contract_id is not None:
        lines += "contract_id%s%s\n" % (sep, contract_id)
    if replaces is not None:
        lines += "replaces%s%s\n" % (sep, replaces)
    return first + "\n" + lines + extra + rest


def spaced(**kw):
    return with_links_text(RF_SPACED, "    ", **kw)


def colon(**kw):
    return with_links_text(RF_COLON, ": ", **kw)


class Base(unittest.TestCase):
    def run_skeleton(self, *flags):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contract.txt"
            path.write_text(SKEL_CONTRACT)
            proc = subprocess.run([sys.executable, "-B", str(SCRIPTS / "handoff_skeleton.py"),
                                   *flags, str(path)], capture_output=True, text=True, cwd=tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def fill_json(self, out, replaces):
        obj = json.loads(out)
        obj["id"] = "HND-20261123-01"
        obj["from"] = "worker"
        obj["date"] = "2026-11-23"
        obj["state"] = "DONE"
        obj["readback"] = "task: x | limits: y | done when: ACC-1, ACC-2"
        obj["summary"] = "Done."
        obj["changes"] = ["none"]
        obj["commit"] = "none"
        obj["checks"] = [c.replace("<n>", "0").replace("<result>", "ok") for c in obj["checks"]]
        obj["contract"] = {k: "VERIFIED" for k in obj["contract"]}
        obj["open"] = "-"
        obj["issues"] = ["-"]
        obj["next"] = "Accept."
        obj["replaces"] = replaces
        return json.dumps(obj)

    def fill_text(self, out, replaces):
        text = out
        for old, new in (("<YYYYMMDD>", "20261123"), ("<NN>", "01"), ("<YYYY-MM-DD>", "2026-11-23"),
                         ("<agent role>", "worker"), ("<DONE, PARTIAL, BLOCKED or UNABLE>", "DONE"),
                         ("<what I will do>", "x"), ("<what I must not change>", "y"),
                         ("<1 to 3 sentences>", "Done."), ("<file: what changed>", "none"),
                         ("<repo@sha or none>", "none"), ("<n>", "0"), ("<result>", "ok"),
                         ("<VERIFIED, FAILED or UNVERIFIED>", "VERIFIED"),
                         ("<the work that remains and why, or —>", "-"),
                         ("<severity: description, or —>", "-"),
                         ("<the first step for the receiver>", "Accept."),
                         (PLACEHOLDER, replaces)):
            text = text.replace(old, new)
        return text


class SkeletonKeyTests(Base):
    """ACC-1: the position of both keys in the JSON skeleton and in the text skeleton."""

    def test_json_skeleton_holds_both_keys_directly_after_id(self):
        pairs = json.loads(self.run_skeleton(), object_pairs_hook=lambda p: p)
        self.assertEqual([k for k, _ in pairs][:4], ["id", "contract_id", "replaces", "from"])
        self.assertEqual([k for k, _ in pairs][3:], list(vh.REQUIRED_KEYS)[1:])
        values = dict(pairs)
        self.assertEqual(values["contract_id"], "CON-20261123-90")
        self.assertEqual(values["replaces"], PLACEHOLDER)

    def test_text_skeleton_holds_both_keys_directly_after_id(self):
        lines = self.run_skeleton("--text").split("\n")
        self.assertEqual(lines[0].split()[0], "id")
        self.assertEqual(lines[1].split(None, 1), ["contract_id", "CON-20261123-90"])
        self.assertEqual(lines[2].split(None, 1), ["replaces", PLACEHOLDER])
        self.assertEqual(lines[3].split()[0], "from")
        parsed = vh.parse_handoff("\n".join(lines))
        self.assertEqual([k for k, _ in parsed["pairs"]],
                         ["id", "contract_id", "replaces"] + list(vh.REQUIRED_KEYS)[1:])
        self.assertEqual(parsed["problems"], [])
        self.assertEqual(parsed["values"]["contract_id"], "CON-20261123-90")
        self.assertEqual(parsed["values"]["replaces"], PLACEHOLDER)

    def test_filled_json_skeleton_with_none_is_valid(self):
        result = vh.validate(SKEL_CONTRACT, self.fill_json(self.run_skeleton(), "none"))
        self.assertIs(result["valid"], True, result["findings"])

    def test_filled_json_skeleton_with_an_hnd_id_is_valid(self):
        result = vh.validate(SKEL_CONTRACT, self.fill_json(self.run_skeleton(), "HND-20261122-03"))
        self.assertIs(result["valid"], True, result["findings"])

    def test_filled_text_skeleton_with_none_is_valid(self):
        result = vh.validate(SKEL_CONTRACT, self.fill_text(self.run_skeleton("--text"), "none"))
        self.assertIs(result["valid"], True, result["findings"])

    def test_filled_text_skeleton_with_an_hnd_id_is_valid(self):
        result = vh.validate(SKEL_CONTRACT, self.fill_text(self.run_skeleton("--text"), "HND-20261122-03"))
        self.assertIs(result["valid"], True, result["findings"])

    def test_placeholder_left_in_json_is_a_defect(self):
        out = self.run_skeleton()
        obj = json.loads(self.fill_json(out, PLACEHOLDER))
        result = vh.validate(SKEL_CONTRACT, json.dumps(obj))
        self.assertIs(result["valid"], False)
        self.assertEqual([f["level"] for f in v7(result)], ["defect"])
        self.assertIn("placeholder", v7(result)[0]["message"])

    def test_placeholder_left_in_text_is_a_defect(self):
        result = vh.validate(SKEL_CONTRACT, self.fill_text(self.run_skeleton("--text"), PLACEHOLDER))
        self.assertIs(result["valid"], False)
        self.assertEqual([f["level"] for f in v7(result)], ["defect"])
        self.assertIn("placeholder", v7(result)[0]["message"])


class ValueTests(Base):
    """ACC-2: the value of each key."""

    FORMS = (("json", with_links_json), ("spaced", spaced), ("colon", colon))

    def each_form(self, **kw):
        for name, build in self.FORMS:
            yield name, vh.validate(RF_CONTRACT, build(**kw))

    def test_the_fixtures_with_good_links_are_valid(self):
        for replaces in ("none", "HND-20261116-02", "HND-20261116-123456"):
            for name, result in self.each_form(replaces=replaces):
                with self.subTest(form=name, replaces=replaces):
                    self.assertIs(result["valid"], True, result["findings"])
                    self.assertEqual(v7(result), [])

    def test_contract_id_that_is_no_con_id(self):
        for bad in ("foo", "CON-2026-01", "CON-20261117-1", "CON-20261117-1234567",
                    "HND-20261117-01", "CON-20261117-01x", "con-20261117-01",
                    "CON-<YYYYMMDD>-<NN>", "CON-２０２６１１１７-01", "CON-20261117-01 CON-20261117-01"):
            for name, result in self.each_form(contract_id=bad):
                with self.subTest(form=name, value=bad):
                    self.assertIs(result["valid"], False)
                    found = v7(result)
                    self.assertEqual([f["level"] for f in found], ["defect"], found)
                    self.assertIn("not a CON id", found[0]["message"])

    def test_empty_contract_id(self):
        for name, result in self.each_form(contract_id=""):
            with self.subTest(form=name):
                self.assertIs(result["valid"], False)
                self.assertIn("not a CON id", v7(result)[0]["message"])

    def test_contract_id_that_differs_from_the_contract(self):
        for other in ("CON-20261117-02", "CON-20261118-01", "CON-20261117-000001"):
            for name, result in self.each_form(contract_id=other):
                with self.subTest(form=name, value=other):
                    self.assertIs(result["valid"], False)
                    found = v7(result)
                    self.assertEqual([f["level"] for f in found], ["defect"], found)
                    self.assertIn("differs from the contract id", found[0]["message"])

    def test_contract_id_that_equals_the_contract_is_no_defect(self):
        for name, result in self.each_form(contract_id=CON):
            with self.subTest(form=name):
                self.assertEqual(v7(result), [])

    def test_replaces_that_is_neither_form(self):
        for bad in ("None", "NONE", "x", "HND-1", "HND-20261116-1", "HND-20261116-1234567",
                    "CON-20261116-02", "hnd-20261116-02", "none none", "HND-20261116-02x",
                    "HND-２０２６１１１６-02"):
            for name, result in self.each_form(replaces=bad):
                with self.subTest(form=name, value=bad):
                    self.assertIs(result["valid"], False)
                    found = v7(result)
                    self.assertEqual([f["level"] for f in found], ["defect"], found)
                    self.assertIn("neither", found[0]["message"])

    def test_empty_replaces(self):
        for name, result in self.each_form(replaces=""):
            with self.subTest(form=name):
                self.assertIs(result["valid"], False)
                self.assertIn("neither", v7(result)[0]["message"])

    def test_replaces_that_equals_the_own_id(self):
        for name, result in self.each_form(replaces=OWN):
            with self.subTest(form=name):
                self.assertIs(result["valid"], False)
                found = v7(result)
                self.assertEqual([f["level"] for f in found], ["defect"], found)
                self.assertIn("own id", found[0]["message"])

    def test_replaces_that_is_the_placeholder(self):
        for name, result in self.each_form(replaces=PLACEHOLDER):
            with self.subTest(form=name):
                self.assertIs(result["valid"], False)
                self.assertIn("placeholder", v7(result)[0]["message"])

    def test_a_json_value_that_is_no_string(self):
        for raw in ("5", "null", "true"):
            text = RF_JSON.replace('  "from"', '  "replaces": %s,\n  "from"' % raw, 1)
            with self.subTest(raw=raw):
                result = vh.validate(RF_CONTRACT, text)
                self.assertIs(result["valid"], False)
                self.assertEqual(len(v7(result)), 1)

    def test_a_contract_id_with_a_broken_contract_is_still_checked_for_form(self):
        result = vh.validate("", with_links_json(contract_id="foo"))
        self.assertTrue(any("not a CON id" in f["message"] for f in v7(result)))


class RepeatAndColonTests(Base):
    """ACC-3: the colon key form and a key that stands twice."""

    def twice(self, form, key):
        if form == "json":
            line = '  "%s": %s,\n' % (key, json.dumps("none" if key == "replaces" else CON))
            kw = {"contract_id": None if key == "contract_id" else CON,
                  "replaces": None if key == "replaces" else "none"}
            return with_links_json(extra=line * 2, **kw)
        value = "none" if key == "replaces" else CON
        sep = "    " if form == "spaced" else ": "
        line = "%s%s%s\n" % (key, sep, value)
        kw = {"contract_id": None if key == "contract_id" else CON,
              "replaces": None if key == "replaces" else "none"}
        return with_links_text(RF_SPACED if form == "spaced" else RF_COLON, sep, extra=line * 2, **kw)

    def test_each_key_once_in_each_form_is_valid(self):
        for build in (with_links_json, spaced, colon):
            result = vh.validate(RF_CONTRACT, build())
            self.assertIs(result["valid"], True, result["findings"])

    def test_each_key_twice_in_each_form_is_a_defect(self):
        for form in ("json", "spaced", "colon"):
            for key in ("contract_id", "replaces"):
                with self.subTest(form=form, key=key):
                    result = vh.validate(RF_CONTRACT, self.twice(form, key))
                    self.assertIs(result["valid"], False)
                    found = v7(result)
                    self.assertEqual([f["level"] for f in found], ["defect"], found)
                    self.assertIn("`%s`" % key, found[0]["message"])
                    self.assertIn("2 times", found[0]["message"])

    def test_a_key_twice_with_two_different_values_is_a_defect(self):
        text = colon(extra="replaces: HND-20261116-02\n")
        result = vh.validate(RF_CONTRACT, text)
        self.assertIs(result["valid"], False)
        self.assertTrue(any("2 times" in f["message"] for f in v7(result)))

    def test_both_keys_in_the_colon_form_are_read_as_keys(self):
        parsed = vh.parse_handoff(colon(replaces="HND-20261116-02"))
        self.assertEqual([k for k, _ in parsed["pairs"]][:3], ["id", "contract_id", "replaces"])
        self.assertEqual(parsed["values"]["contract_id"], CON)
        self.assertEqual(parsed["values"]["replaces"], "HND-20261116-02")
        self.assertEqual(parsed["problems"], [])
        result = vh.validate(RF_CONTRACT, colon(replaces="HND-20261116-02"))
        self.assertIs(result["valid"], True, result["findings"])
        self.assertEqual(result["findings"], [])

    def test_the_value_of_a_colon_key_keeps_the_rest_of_the_line(self):
        parsed = vh.parse_handoff(colon(contract_id="  CON-20261117-01 \t"))
        self.assertEqual(parsed["values"]["contract_id"], CON)
        parsed = vh.parse_handoff(colon(replaces="x: y"))
        self.assertEqual(parsed["values"]["replaces"], "x: y")

    def test_a_colon_key_is_checked_in_the_colon_form(self):
        result = vh.validate(RF_CONTRACT, colon(contract_id="CON-20261117-02", replaces="foo"))
        self.assertIs(result["valid"], False)
        messages = " ".join(f["message"] for f in v7(result))
        self.assertIn("differs from the contract id", messages)
        self.assertIn("neither", messages)

    def test_a_colon_key_among_spaced_keys_is_no_unresolved_line(self):
        text = RF_SPACED.replace("id        HND-20261117-01\n",
                                 "id        HND-20261117-01\ncontract_id: CON-20261117-01\nreplaces: none\n")
        self.assertIn("contract_id: CON", text)
        parsed = vh.parse_handoff(text)
        self.assertEqual(parsed["values"]["contract_id"], CON)
        self.assertEqual(parsed["values"]["replaces"], "none")
        self.assertEqual(parsed["problems"], [])

    def test_the_handoff_keys_of_the_colon_form_are_still_read(self):
        result = vh.validate(RF_CONTRACT, colon())
        self.assertEqual([f for f in result["findings"] if f["rule"] != "V7"], [])


def main_validator():
    """The validator of main 9a6c271, or None when the repository cannot give it."""
    proc = subprocess.run(["git", "-C", str(ROOT), "show", "%s:scripts/validate_handoff.py" % MAIN_SHA],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "validate_handoff_main.py"
    path.write_text(proc.stdout)
    spec = importlib.util.spec_from_file_location("validate_handoff_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tmp.cleanup()
    return module


class NoLinkKeyTests(unittest.TestCase):
    """ACC-4: a handoff without the keys gives the result of main 9a6c271."""

    CASES = (
        ("handoff_ok", FIX / "validate_handoff" / "contract.txt", FIX / "validate_handoff" / "handoff_ok.txt"),
        ("record_frame json", FIX / "record_frame" / "contract.txt", FIX / "record_frame" / "handoff.json"),
    )
    MORE = (
        ("handoff_cut", FIX / "validate_handoff" / "contract.txt", FIX / "validate_handoff" / "handoff_cut.txt"),
        ("handoff_broken", FIX / "validate_handoff" / "contract.txt", FIX / "validate_handoff" / "handoff_broken.txt"),
        ("record_frame colon", FIX / "record_frame" / "contract.txt", FIX / "record_frame" / "handoff-colon.txt"),
        ("record_frame spaced", FIX / "record_frame" / "contract.txt", FIX / "record_frame" / "handoff.txt"),
        ("record_frame lead", FIX / "record_frame" / "contract.txt", FIX / "record_frame" / "handoff.txt"),
    )

    def test_the_two_fixtures_are_valid_with_no_finding(self):
        for name, contract, handoff in self.CASES:
            with self.subTest(name):
                result = vh.validate(contract.read_text(), handoff.read_text())
                self.assertIs(result["valid"], True)
                self.assertEqual(result["findings"], [])

    def test_the_two_fixtures_give_the_result_of_main(self):
        old = main_validator()
        if old is None:
            self.skipTest("commit %s is not in this repository" % MAIN_SHA)
        for name, contract, handoff in self.CASES + self.MORE:
            with self.subTest(name):
                text = handoff.read_text()
                if name == "record_frame lead":
                    text = text + RF_LEAD
                new_result = vh.validate(contract.read_text(), text)
                old_result = old.validate(contract.read_text(), text)
                self.assertEqual(new_result["valid"], old_result["valid"])
                self.assertEqual(new_result["findings"], old_result["findings"])

    def test_no_finding_names_rule_v7_without_the_keys(self):
        for name, contract, handoff in self.CASES + self.MORE:
            with self.subTest(name):
                result = vh.validate(contract.read_text(), handoff.read_text())
                self.assertEqual(v7(result), [])

    def test_the_version_is_2_3_0(self):
        self.assertEqual(vh.VALIDATOR_VERSION, "2.3.0")
        self.assertEqual(vh.validate(RF_CONTRACT, RF_JSON)["validator_version"], "2.3.0")


class StoredFrameTests(unittest.TestCase):
    """A stored record frame (envelope and lead marker) around a handoff that holds both keys."""

    def bodies(self):
        return (("json", with_links_json()), ("spaced", spaced()), ("colon", colon()))

    def test_a_framed_handoff_gives_the_result_of_the_bare_handoff(self):
        for name, body in self.bodies():
            with self.subTest(name):
                bare = vh.validate(RF_CONTRACT, body)
                framed = vh.validate(ENVELOPE + RF_CONTRACT,
                                     ENVELOPE + body.rstrip("\n") + "\n\n" + RF_LEAD)
                self.assertIs(bare["valid"], True, bare["findings"])
                self.assertEqual(framed["valid"], bare["valid"])
                self.assertEqual(framed["findings"], bare["findings"])

    def test_a_framed_handoff_with_a_bad_link_has_the_same_defect(self):
        for name, build in (("json", with_links_json), ("spaced", spaced), ("colon", colon)):
            with self.subTest(name):
                body = build(contract_id="CON-20261117-02", replaces=OWN)
                bare = vh.validate(RF_CONTRACT, body)
                framed = vh.validate(ENVELOPE + RF_CONTRACT, ENVELOPE + body.rstrip("\n") + "\n\n" + RF_LEAD)
                self.assertIs(framed["valid"], False)
                self.assertEqual(len(v7(framed)), 2)
                self.assertEqual(framed["findings"], bare["findings"])

    def test_a_link_key_below_the_lead_marker_is_not_read(self):
        body = RF_COLON.rstrip("\n") + "\n\n" + RF_LEAD + "replaces: foo\ncontract_id: foo\n"
        result = vh.validate(RF_CONTRACT, body)
        self.assertEqual(v7(result), [])
        self.assertIs(result["valid"], True, result["findings"])


if __name__ == "__main__":
    unittest.main()
