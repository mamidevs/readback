#!/usr/bin/env python3
"""Offline checks for scripts/handoff_skeleton.py (CON-20261123-06).

Run: python3 -m unittest discover -s checks -p 'test_handoff_skeleton.py' -v
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(os.environ.get("SKELETON_ROOT") or Path(__file__).resolve().parent.parent)
SCRIPT = ROOT / "scripts" / "handoff_skeleton.py"
sys.path.insert(0, str(ROOT / "scripts"))
sys.dont_write_bytecode = True
import validate_handoff  # noqa: E402

KEYS = ["id", "from", "date", "state", "readback", "summary", "changes",
        "commit", "checks", "contract", "open", "issues", "next"]
# the skeleton holds the two link keys of CON-20261123-07 directly after `id`
SKELETON_KEYS = KEYS[:1] + ["contract_id", "replaces"] + KEYS[1:]
STATUS_PLACEHOLDER = "<VERIFIED, FAILED or UNVERIFIED>"
REPLACES_PLACEHOLDER = "<HND id or none>"

CHECK_TEXTS = {
    "double quotes": 'echo "a" "b"',
    "single quotes": "echo 'a' 'b'",
    "backslash": r"grep a\.b file",
    "two characters backslash n": r'printf "a\nb"',
    "vertical bar": "cat f | wc -l",
    "sh -c": 'sh -c "a && b"',
}


def contract_text(checks, out="Out of scope: nothing"):
    lines = ["CON-20261123-90  Do a thing."]
    for i, check in enumerate(checks, 1):
        lines.append("ACC-%d  The tool shall do %d.  Check: %s" % (i, i, check))
    lines.append(out)
    return "\n".join(lines) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text, name="contract.txt", raw=None):
        path = Path(self.tmp.name) / name
        path.write_bytes(raw if raw is not None else text.encode("utf-8"))
        return path

    def run_script(self, *args):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *map(str, args)],
                              capture_output=True, text=True, cwd=self.tmp.name)

    def skeleton(self, text, *flags):
        proc = self.run_script(*flags, self.write(text))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        return proc.stdout

    def fill_json(self, out, results=None):
        obj = json.loads(out)
        obj["id"] = "HND-20261123-01"
        obj["replaces"] = "none"
        obj["from"] = "worker"
        obj["date"] = "2026-11-23"
        obj["state"] = "DONE"
        obj["readback"] = obj["readback"].replace("<what I will do>", "x").replace(
            "<what I must not change>", "y")
        obj["summary"] = "Done."
        obj["changes"] = ["none"]
        obj["commit"] = "none"
        obj["checks"] = [c.replace("<n>", "0").replace("<result>", "ok") for c in obj["checks"]]
        obj["contract"] = {k: "VERIFIED" for k in obj["contract"]}
        obj["open"] = "-"
        obj["issues"] = ["-"]
        obj["next"] = "Accept."
        return obj

    def fill_text(self, out, summary="Done."):
        text = out
        for old, new in (("<YYYYMMDD>", "20261123"), ("<NN>", "01"), ("<YYYY-MM-DD>", "2026-11-23"),
                         ("<agent role>", "worker"), ("<DONE, PARTIAL, BLOCKED or UNABLE>", "DONE"),
                         ("<what I will do>", "x"), ("<what I must not change>", "y"),
                         ("<1 to 3 sentences>", summary), ("<file: what changed>", "none"),
                         ("<repo@sha or none>", "none"), (REPLACES_PLACEHOLDER, "none"), ("<n>", "0"), ("<result>", "ok"),
                         (STATUS_PLACEHOLDER, "VERIFIED"),
                         ("<the work that remains and why, or —>", "-"),
                         ("<severity: description, or —>", "-"),
                         ("<the first step for the receiver>", "Accept.")):
            text = text.replace(old, new)
        return text


class KeyOrderTests(Base):
    def test_keys_in_protocol_order(self):
        out = self.skeleton(contract_text(["sh a.sh"]))
        obj = json.loads(out, object_pairs_hook=lambda p: p)
        self.assertEqual([k for k, _ in obj], SKELETON_KEYS)
        self.assertEqual(list(validate_handoff.REQUIRED_KEYS), KEYS)

    def test_prints_one_json_object(self):
        out = self.skeleton(contract_text(["sh a.sh"]))
        self.assertTrue(out.endswith("\n"))
        self.assertEqual(out.count("\n"), out.rstrip("\n").count("\n") + 1)
        self.assertIsInstance(json.loads(out), dict)

    def test_text_option_keys_in_order(self):
        out = self.skeleton(contract_text(["sh a.sh"]), "--text")
        parsed = validate_handoff.parse_handoff(out)
        self.assertEqual(parsed["form"], "text")
        self.assertEqual([k for k, _ in parsed["pairs"]], SKELETON_KEYS)
        self.assertEqual(parsed["problems"], [])
        self.assertFalse(out.lstrip().startswith("{"))

    def test_text_form_holds_every_value_of_the_json_form(self):
        text = contract_text(["sh a.sh", 'sh -c "a && b"', "sh a.sh"])
        obj = json.loads(self.skeleton(text))
        parsed = validate_handoff.parse_handoff(self.skeleton(text, "--text"))
        for key in SKELETON_KEYS:
            value = obj[key]
            if isinstance(value, dict):
                value = "\n".join("%s %s" % item for item in value.items())
            elif isinstance(value, list):
                value = "\n".join(value)
            with self.subTest(key):
                self.assertEqual(parsed["values"][key], value)


class ChecksTests(Base):
    def test_one_string_for_each_distinct_check_in_order(self):
        out = self.skeleton(contract_text(["sh b.sh", "sh a.sh"]))
        self.assertEqual(json.loads(out)["checks"], [
            "sh b.sh | exit <n> | <result>", "sh a.sh | exit <n> | <result>"])

    def test_one_string_when_two_statements_share_a_check(self):
        out = self.skeleton(contract_text(["sh a.sh", "sh a.sh", "sh b.sh"]))
        obj = json.loads(out)
        self.assertEqual(obj["checks"], ["sh a.sh | exit <n> | <result>", "sh b.sh | exit <n> | <result>"])
        self.assertEqual(sorted(obj["contract"]), ["ACC-1", "ACC-2", "ACC-3"])

    def test_same_check_again_later_is_still_one_string(self):
        out = self.skeleton(contract_text(["sh a.sh", "sh b.sh", "sh a.sh"]))
        self.assertEqual(len(json.loads(out)["checks"]), 2)

    def test_two_distinct_checks_give_two_strings(self):
        out = self.skeleton(contract_text(["sh a.sh", "sh b.sh"]))
        self.assertEqual(len(json.loads(out)["checks"]), 2)

    def test_text_form_checks_equal_json_form(self):
        text = contract_text(["sh a.sh", "sh a.sh", 'sh -c "a && b"'])
        as_json = json.loads(self.skeleton(text))["checks"]
        parsed = validate_handoff.parse_handoff(self.skeleton(text, "--text"))
        self.assertEqual([c["text"] for c in parsed["checks_lines"]], as_json)


class ContractKeyTests(Base):
    def test_every_acc_id_with_the_placeholder(self):
        out = self.skeleton(contract_text(["sh a.sh", "sh b.sh", "sh c.sh"]))
        self.assertEqual(json.loads(out)["contract"], {
            "ACC-1": STATUS_PLACEHOLDER, "ACC-2": STATUS_PLACEHOLDER, "ACC-3": STATUS_PLACEHOLDER})

    def test_text_form_every_acc_id(self):
        out = self.skeleton(contract_text(["sh a.sh", "sh b.sh"]), "--text")
        parsed = validate_handoff.parse_handoff(out)
        entries, unparsed = validate_handoff.parse_statuses(parsed["values"]["contract"])
        self.assertEqual([e[0] for e in entries], [])
        self.assertEqual(unparsed, ["ACC-1 " + STATUS_PLACEHOLDER, "ACC-2 " + STATUS_PLACEHOLDER])

    def test_readback_names_the_acc_ids(self):
        out = json.loads(self.skeleton(contract_text(["sh a.sh", "sh b.sh"])))
        self.assertTrue(out["readback"].endswith("done when: ACC-1, ACC-2"))

    def test_contract_in_the_json_form(self):
        text = json.dumps({"id": "CON-20261123-91", "task": "t", "out_of_scope": "n",
                           "ACC-1": "The tool shall work.  Check: sh a.sh",
                           "ACC-2": {"statement": "It shall pass.", "check": 'sh -c "a && b"'}})
        obj = json.loads(self.skeleton(text))
        self.assertEqual(obj["checks"], ["sh a.sh | exit <n> | <result>",
                                         'sh -c "a && b" | exit <n> | <result>'])
        self.assertEqual(sorted(obj["contract"]), ["ACC-1", "ACC-2"])

    def test_contract_in_the_json_form_acc_list(self):
        text = json.dumps({"id": "CON-20261123-92", "task": "t", "acc": [
            {"id": "ACC-1", "statement": "s", "check": "sh a.sh"}]})
        self.assertEqual(json.loads(self.skeleton(text))["checks"], ["sh a.sh | exit <n> | <result>"])


class RoundTripTests(Base):
    def test_check_text_round_trips_exactly(self):
        for name, check in CHECK_TEXTS.items():
            with self.subTest(name):
                text = contract_text([check])
                contract = validate_handoff.parse_contract(text)
                obj = json.loads(self.skeleton(text))
                self.assertEqual(len(obj["checks"]), 1)
                self.assertEqual(obj["checks"][0].split(" | exit ")[0], contract["statements"][0]["check"])
                self.assertEqual(obj["checks"][0].split(" | exit ")[0], check)

    def test_filled_json_form_is_valid(self):
        for name, check in CHECK_TEXTS.items():
            with self.subTest(name):
                text = contract_text([check, "sh b.sh"])
                obj = self.fill_json(self.skeleton(text))
                result = validate_handoff.validate(text, json.dumps(obj))
                self.assertIs(result["valid"], True, result["findings"])

    def test_filled_text_form_is_valid(self):
        for name, check in CHECK_TEXTS.items():
            with self.subTest(name):
                text = contract_text([check, "sh b.sh"])
                filled = self.fill_text(self.skeleton(text, "--text"))
                result = validate_handoff.validate(text, filled)
                self.assertIs(result["valid"], True, result["findings"])

    def test_all_check_texts_in_one_contract(self):
        checks = list(CHECK_TEXTS.values())[:5]
        text = contract_text(checks)
        self.assertIs(validate_handoff.validate(
            text, json.dumps(self.fill_json(self.skeleton(text))))["valid"], True)
        self.assertIs(validate_handoff.validate(
            text, self.fill_text(self.skeleton(text, "--text")))["valid"], True)

    def test_text_form_after_a_filled_value_with_a_continuation_line(self):
        text = contract_text(["sh a.sh", "sh b.sh"])
        filled = self.fill_text(self.skeleton(text, "--text"), summary="Done.\n          Second line.")
        result = validate_handoff.validate(text, filled)
        self.assertIs(result["valid"], True, result["findings"])
        parsed = validate_handoff.parse_handoff(filled)
        self.assertEqual(parsed["values"]["summary"], "Done.\nSecond line.")
        self.assertEqual([k for k, _ in parsed["pairs"]], SKELETON_KEYS)


class KnownLimitTests(Base):
    def test_backslash_n_changed_to_newline_gives_unparsed_with_v6(self):
        text = contract_text([CHECK_TEXTS["two characters backslash n"]])
        raw = self.skeleton(text)
        self.assertIn("\\\\n", raw)
        broken = raw.replace("\\\\n", "\\n")
        obj = self.fill_json(broken)
        result = validate_handoff.validate(text, json.dumps(obj))
        self.assertEqual(result["valid"], "unparsed")
        self.assertIn("V6", [f["rule"] for f in result["findings"]])
        intact = self.fill_json(raw)
        self.assertIs(validate_handoff.validate(text, json.dumps(intact))["valid"], True)


class ExitTwoTests(Base):
    def assert_refused(self, proc):
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(len(proc.stderr.splitlines()), 1)
        self.assertTrue(proc.stderr.strip())

    def test_unreadable_file(self):
        self.assert_refused(self.run_script(Path(self.tmp.name) / "missing.txt"))

    def test_directory_is_unreadable(self):
        self.assert_refused(self.run_script(self.tmp.name))

    def test_not_utf8_is_unreadable(self):
        self.assert_refused(self.run_script(self.write("", raw=b"CON-20261123-90 x\n\xff\xfe\n")))

    def test_no_statement(self):
        for flags in ((), ("--text",)):
            with self.subTest(flags):
                path = self.write("CON-20261123-90  Do a thing.\nOut of scope: nothing\n")
                self.assert_refused(self.run_script(*flags, path))

    def test_empty_file_and_empty_json_object(self):
        self.assert_refused(self.run_script(self.write("", "a.txt")))
        self.assert_refused(self.run_script(self.write("{}", "b.txt")))

    def test_check_with_a_continuation_line(self):
        text = "CON-20261123-90  t\nACC-1  The tool shall work.  Check: sh -c \"a\n   && b\"\nOut of scope: n\n"
        self.assert_refused(self.run_script(self.write(text)))

    def test_json_check_with_a_line_break(self):
        text = json.dumps({"id": "CON-20261123-90", "task": "t",
                           "ACC-1": {"statement": "s", "check": "a\nb"}})
        self.assert_refused(self.run_script(self.write(text)))

    def test_one_bad_check_among_good_ones_gives_nothing(self):
        text = ("CON-20261123-90  t\nACC-1  s  Check: sh a.sh\n"
                "ACC-2  s  Check: sh b.sh\n   more\nOut of scope: n\n")
        self.assert_refused(self.run_script(self.write(text)))

    def test_statement_without_check(self):
        self.assert_refused(self.run_script(self.write("CON-20261123-90  t\nACC-1  s\nOut of scope: n\n")))

    def test_usage_error(self):
        proc = self.run_script()
        self.assert_refused(proc)
        self.assert_refused(self.run_script("a", "b", "c"))


class CommandTests(Base):
    def test_run_as_a_command_exit_zero(self):
        path = self.write(contract_text(["sh a.sh"]))
        proc = self.run_script(path)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout)["checks"], ["sh a.sh | exit <n> | <result>"])

    def test_text_option_before_or_after_path(self):
        path = self.write(contract_text(["sh a.sh"]))
        a, b = self.run_script("--text", path), self.run_script(path, "--text")
        self.assertEqual((a.returncode, b.returncode), (0, 0))
        self.assertEqual(a.stdout, b.stdout)

    def test_script_writes_no_file_next_to_itself(self):
        before = sorted(p.name for p in (ROOT / "scripts").iterdir())
        self.run_script(self.write(contract_text(["sh a.sh"])))
        self.assertEqual(sorted(p.name for p in (ROOT / "scripts").iterdir()), before)


if __name__ == "__main__":
    unittest.main()
