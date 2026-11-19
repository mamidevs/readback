"""Offline end-to-end checks for credential masking at review output boundaries."""

import hashlib
import json
import unittest

import test_judge_modes as modes
import judge_rounds
from review_redaction import MASK, redact, redact_prefix, redact_value


class RedactionTests(unittest.TestCase):
    def test_recognizable_forms_and_ordinary_evidence(self):
        examples = (
            ('DB_PASSWORD="two words"', 'DB_PASSWORD="<secret>"'),
            ("'accessToken': 'first\\' and second'", "'accessToken': '<secret>'"),
            (r"PWD=two\ words", "PWD=<secret>"),
            ("--api-key=abc", "--api-key=<secret>"),
            ("  db_password: |-\n    many\n    words\n  next: OK", "  db_password: <secret>\n  next: OK"),
            (r'output password=\"escaped phrase\"', "output password=<secret>"),
            ("Authorization: Basic dXNlcjpwYXNz==", "Authorization: <secret>"),
            ("Authorization: Bearer a.b.c", "Authorization: <secret>"),
            ("Bearer eyJ0eXAi.e30.ABCD==", "Bearer <secret>"),
            ("pg://bob:p%40ss@db/alpha", "pg://<secret>@db/alpha"),
            ("postgres://pass@db", "postgres://<secret>@db"),
            ("private_key='a\nb'", "private_key='<secret>'"),
            ("before\n-----BEGIN RSA PRIVATE KEY-----\nbytes\n-----END RSA PRIVATE KEY-----\nafter",
             "before\n<secret>\nafter"),
            ("-----BEGIN PRIVATE KEY-----\nunclosed", MASK),
            ("git ghp_exampleToken1234", "git <secret>"),
            ("say sk-proj-fakelongvalue", "say <secret>"),
        )
        for raw, expected in examples:
            with self.subTest(kind=raw.split()[0]):
                self.assertEqual(redact(raw), expected)
                self.assertEqual(redact(expected), expected)
        for fragment in ("PASS pg://user:truncated", "FAIL ghp_ab", "PASS Bearer ab", 'PASS password="abc' + chr(92)):
            self.assertIn(MASK, redact_prefix(fragment))
        plain = "Basic configuration, token counts, API key format and password policy. sha256 ba7809"
        self.assertEqual(redact(plain), plain)
        nested = {"evidence": [{'apiKey': "secret with spaces"}, 'db_password="inside quoted JSON"'],
                  "call": {"attempt": 2, "authorization": "ev-2", "fails_before": 1}}
        safe = redact_value(nested)
        self.assertEqual(json.loads(redact(json.dumps(nested))), safe)
        self.assertEqual(safe["evidence"], [{"apiKey": MASK}, 'db_password="<secret>"'])
        self.assertEqual(safe["call"], nested["call"])


class RecordPrivacyTests(modes.JudgeCase):
    def test_provider_call_report_receipt_copies_and_raw_input_bindings(self):
        marker = "exampleFixtureSecret943"
        field = 'API_KEY="' + marker + ' extra"'
        self.put(modes.CONTRACT_TRUE, modes.HANDOFF_TRUE.replace("Done.", "Done. " + field))
        coverage = self.repo / ("coverage-token=" + marker)
        coverage.write_text("public evidence")
        listed = self.base / "coverage.list"
        listed.write_text(str(coverage) + "\n")
        self.contract = self.contract.rename(self.contract.with_name("contract-password=" + marker))
        answer = "FAIL Found a credential.\n" + field + "\n"
        result = self.run_helper(env={"FAKE_ANSWER": answer},
                                 args=[str(self.contract), str(self.handoff), str(self.repo), "--coverage", str(listed)])
        self.assertEqual((result.code, result.word), (0, "FAIL"), result.out + result.err)
        self.assertNotIn(marker, result.out + result.err)
        record = self.records("completed")[0]
        self.assertEqual(record["answer"], redact(answer))
        self.assertEqual(record["answer_sha256"], hashlib.sha256(redact(answer).encode()).hexdigest())
        for key, path in (("contract", self.contract), ("handoff", self.handoff)):
            self.assertEqual(record[key]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(record["call"]["coverage"][0]["sha256"], hashlib.sha256(coverage.read_bytes()).hexdigest())
        rounds = self.home / "logbook" / ".readback" / "rounds"
        self.assertEqual(next(rounds.rglob("answer.txt")).read_text(), record["answer"])
        self.assertIn(MASK, next(rounds.rglob("observation.txt")).read_text())
        self.assertIn(MASK, next(rounds.rglob("handoff.txt")).read_text())
        for folder in (self.common / "readback", rounds):
            for path in folder.rglob("*"):
                if path.is_file():
                    self.assertNotIn(marker.encode(), path.read_bytes(), path.name)

        # A retry can encounter files from an older installation. Mask before diffing
        # and before clipping the previous answer to the section byte budget.
        old = "legacyFixtureSecret527"
        for name in ("handoff.txt", "observation.txt"):
            next(rounds.rglob(name)).write_text('PASSWORD="' + old + '"\n')
        next(rounds.rglob("answer.txt")).write_text("FAIL\n-----BEGIN PRIVATE KEY-----\n" + old * 2000 +
                                                    "\n-----END PRIVATE KEY-----\n")
        retry = self.run_helper(env={"FAKE_ANSWER": "PASS Corrected.\n"})
        self.assertEqual(retry.code, 0, retry.out + retry.err)
        prompt = retry.calls[0]["prompt"].decode()
        self.assertIn(judge_rounds.EARLIER_HEAD, prompt)
        self.assertNotIn(old, prompt)
        self.assertIn("FAIL\n<secret>", prompt)

    def test_failure_off_and_skipped_mask_caller_paths_and_echoed_findings(self):
        marker = "fakeEmbeddedCredential847"
        self.handoff = self.handoff.rename(self.handoff.with_name("handoff-password=" + marker))
        self.run_helper(env={"FAKE_ANSWER": "malformed"})
        failed = self.records("completed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["judge"]["verdict"], "none")
        self.assertNotIn(marker, json.dumps(failed))
        for mode, tier, expected in (("off", "gate", "off"), ("gate", "step", "skipped")):
            self.write_settings({"judge": {"mode": mode}})
            self.put(modes.CONTRACT_TRUE.replace("sh a.sh", "sh API_TOKEN=" + marker), modes.HANDOFF_FALSE)
            result = self.run_helper(tier=tier)
            self.assertEqual((result.code, result.word), (0, expected), result.out)
            self.assertNotIn(marker, result.out + result.err)
            self.assertEqual(result.calls, [])
        records = self.records("no-review")
        self.assertEqual({item["result"] for item in records}, {"off", "skipped"})
        self.assertNotIn(marker, json.dumps(records))


if __name__ == "__main__":
    unittest.main()
