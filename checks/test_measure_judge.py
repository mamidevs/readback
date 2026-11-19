"""Offline CLI integration tests. A local stub supplies Codex events and answers."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ENTRY = Path(__file__).resolve().with_name("outside-judge.sh")
FAKE = r'''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
assert "--ephemeral" in args and "--ignore-user-config" in args and "--ignore-rules" in args
assert any(args[i:i+2] == ["-c", "suppress_unstable_features_warning=true"] for i in range(len(args)))
assert args[args.index("-s")+1] == "read-only" and args[-1] == "-"
assert all(flag in args for flag in ("skip_host_skill_discovery", "shell_tool", "multi_agent", "apps", "plugins", "hooks"))
assert not list(Path.cwd().iterdir())
prompt = sys.stdin.buffer.read().decode()
assert "blind-label" not in prompt and "answer-key.json" not in prompt
for event in ("thread.started", "turn.started"):
    print(json.dumps({"type":event, "thread_id":"private-value-omit"}))
case = prompt.splitlines()[-1]
if case.startswith("privacy"):
    import hashlib
    assert "packet-canary" not in prompt and "token=<secret>" in prompt
    answer_detail = hashlib.sha256(prompt.encode()).hexdigest() + " password=answer-canary"
    if case == "privacy-error":
        print(json.dumps({"type": "ghp_diagnosticcanary"}))
if case.startswith("echo"):
    text = prompt if case == "echo" else "different user message"
    print(json.dumps({"type":"item.completed", "item":{"type":"user_message", "text":text}}))
if case == "item-error":
    print(json.dumps({"type":"item.completed", "item":{"type":"error", "message":"private-value-omit"}}))
if case == "unknown-event":
    print(json.dumps({"type":"thread.other", "payload":"private-value-omit"}))
if case == "unknown-item":
    print(json.dumps({"type":"item.completed", "item":{"type":"future_item", "body":"private-value-omit"}}))
model = args[args.index("-m")+1]
if case == "sleep":
    time.sleep(20)
if case == "exit":
    print("usage limit for excluded-account-id", file=sys.stderr)
    sys.exit(7)
if case == "tool":
    print(json.dumps({"type":"item.started", "item":{"type":"command_execution","command":"private-value-omit"}}))
    print(json.dumps({"type":"item.completed", "item":{"type":"command_execution","text":"private-value-omit"}}))
if case == "malformed":
    print("bad-json")
answer = ("PASS" if model == "alpha" else "FAIL") + "  final answer.\r\n"
if case.startswith("privacy"):
    answer = "PASS " + answer_detail
if case == "no-verdict":
    answer = "Maybe"
Path(args[args.index("-o")+1]).write_bytes(answer.encode())
usage = {} if case == "no-tokens" else {"usage": {"input_tokens": 19, "output_tokens": 4}}
if case != "incomplete":
    print(json.dumps({"type":"turn.completed", **usage}))
'''


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        binary = self.base / "codex"
        binary.write_text("#!" + sys.executable + "\n" + FAKE)
        binary.chmod(0o755)

    def measure(self, rows, repeats=1, models=("alpha",), timeout=10):
        cases = []
        for index, (packet, expected) in enumerate(rows):
            name = f"packet-{index}.txt"
            (self.base / name).write_text(packet)
            cases.append({"id": f"blind-label-{index}", "packet": name, "expected": expected})
        manifest = self.base / "answer-key.json"
        manifest.write_text(json.dumps(dict(cases=cases, routes=[dict(model=m, effort="high") for m in models])))
        out = self.base / "out"
        proc = subprocess.run(["bash", str(ENTRY), "--measure", str(manifest), str(out),
                               "--repeats", str(repeats), "--timeout", str(timeout), "--jobs", "2"],
                              cwd=self.base, env=dict(os.environ, PATH=str(self.base) + ":" + os.environ["PATH"]),
                              capture_output=True, text=True, timeout=15)
        return proc, [json.loads(p.read_text()) for p in sorted(out.glob("result-*.json"))]

    def prune(self, batch, *options):
        return subprocess.run(["bash", str(ENTRY), "--measure", "--prune", str(batch), *options],
                              cwd=self.base, capture_output=True, text=True, timeout=5)

    def test_masks_actual_provider_input_and_persisted_outputs(self):
        proc, rows = self.measure([("token=packet-canary\r\nprivacy", "PASS"),
                                   ("token=packet-canary\r\nprivacy-error", "FAIL")])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual([r["outcome"] for r in rows], ["correct_pass", "error"])
        self.assertIn("<secret>", rows[1]["error"])
        for index, row in enumerate(rows, 1):
            record = self.base / "out" / f"input-{index:04d}.json"
            prompt = json.loads(record.read_text())["prompt"]
            self.assertIn("token=<secret>\r\n", prompt)
            digest = hashlib.sha256(prompt.encode()).hexdigest()
            self.assertEqual(row["prompt_sha256"], digest)
            self.assertEqual(row["answer"], f"PASS {digest} password=<secret>")
        for path in (self.base / "out").iterdir():
            for canary in ("packet-canary", "answer-canary", "ghp_diagnosticcanary"):
                self.assertNotIn(canary.encode(), path.read_bytes())

    def test_expire_only_text_after_30_days_and_preserve_metrics(self):
        proc, rows = self.measure([("a", "PASS"), ("no-verdict", "FAIL")])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        batch = (self.base / "out").resolve()
        summary = batch / "summary.json"
        summary_bytes = summary.read_bytes()
        original_inputs = [(batch / f"input-{i:04d}.json").read_bytes() for i in (1, 2)]
        unrelated = batch / "notes.json"
        unrelated.write_text('{"answer":"mine"}')
        for days, apply, count in [(29, True, 0), (30, False, 4), (30, True, 4), (31, True, 0)]:
            with self.subTest(days=days, apply=apply):
                stamp = time.time() - days * 86400
                os.utime(summary, (stamp, stamp))
                result = self.prune(batch, *(["--apply"] if apply else []))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {"applied": apply, "records": count})
                for i, row in enumerate(rows, 1):
                    packet = batch / f"input-{i:04d}.json"
                    report = json.loads((batch / f"result-{i:04d}.json").read_text())
                    if days < 30 or not apply:
                        self.assertEqual(packet.read_bytes(), original_inputs[i - 1])
                        self.assertEqual(report, row)
                    else:
                        self.assertEqual(json.loads(packet.read_text()),
                                         {"case": row["case"], "expected": row["expected"]})
                        self.assertEqual(report, {k: v for k, v in row.items() if k not in ("answer", "error")})
                self.assertEqual(summary.read_bytes(), summary_bytes)
                self.assertEqual(unrelated.read_text(), '{"answer":"mine"}')

    def test_incomplete_unknown_and_linked_batches_are_untouched(self):
        proc, _ = self.measure([("a", "PASS")])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        batch = (self.base / "out").resolve()
        stamp = time.time() - 31 * 86400
        os.utime(batch / "summary.json", (stamp, stamp))
        packet = batch / "input-0001.json"
        record = batch / "result-0001.json"
        before = packet.read_bytes()
        outside = batch.parent / "saved-result.json"
        record.rename(outside)
        self.assertEqual(self.prune(batch, "--apply").returncode, 2)
        record.symlink_to(outside)
        self.assertEqual(self.prune(batch, "--apply").returncode, 2)
        self.assertTrue(record.is_symlink())
        self.assertIn("answer", json.loads(outside.read_text()))
        record.unlink()
        outside.rename(record)
        alias = batch.parent / "alias"
        alias.symlink_to(batch.parent, target_is_directory=True)
        self.assertEqual(self.prune(alias / "out", "--apply").returncode, 2)
        unrelated = batch / "result-0002.json"
        unrelated.write_text('{"answer":"unrelated"}')
        self.assertEqual(self.prune(batch, "--apply").returncode, 2)
        self.assertEqual(unrelated.read_text(), '{"answer":"unrelated"}')
        self.assertEqual(packet.read_bytes(), before)
        self.assertIn("answer", json.loads(record.read_text()))

    def test_repeats_and_confusion_counts_across_routes(self):
        proc, rows = self.measure([("a", "FAIL"), ("b", "PASS")], repeats=2, models=("alpha", "beta"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len({(r["case"], r["model"], r["repeat"]) for r in rows}), 8)
        alpha, beta = json.loads(proc.stdout)
        self.assertEqual((alpha["correct_pass"], alpha["false_accept"], beta["correct_fail"], beta["false_reject"]),
                         (2, 2, 2, 2))
        for r in rows:
            self.assertIsNone(r["error"])
            self.assertGreater(r["elapsed_seconds"], 0)
            self.assertEqual(r["usage"], {"input_tokens": 19, "output_tokens": 4})
            self.assertEqual(r["answer"], r["verdict"] + "  final answer.\r\n")
        for case in {r["case"] for r in rows}:
            self.assertEqual(len({r["prompt_sha256"] for r in rows if r["case"] == case}), 1)
        proc, _ = self.measure([("a", "FAIL")])
        self.assertEqual(proc.returncode, 2)

    def test_invalid_activity_preserves_completion_tokens_and_only_event_names(self):
        cases = ("tool", "echo", "echo-mismatch", "unknown-event", "unknown-item", "item-error")
        proc, rows = self.measure([(c, "PASS") for c in cases])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual([r["outcome"] for r in rows], ["error", "correct_pass", "error", "error", "error", "error"])
        self.assertEqual(rows[0]["events"], ["thread.started", "turn.started", "item.started:command_execution",
                                             "item.completed:command_execution", "turn.completed"])
        self.assertEqual(rows[0]["error"], "unscored activity: command_execution")
        self.assertEqual(rows[-1]["error"], "unscored activity: error")
        for row in rows:
            self.assertEqual(row["completed_turns"], 1)
            self.assertEqual(row["usage"], {"input_tokens": 19, "output_tokens": 4})
        self.assertNotIn("private-value-omit", json.dumps(rows))
        self.assertNotIn("different user message", json.dumps(rows))

    def test_provider_failures_are_unscored_and_missing_usage_stays_unknown(self):
        cases = ("exit", "malformed", "tool", "sleep", "incomplete", "no-verdict", "no-tokens")
        proc, rows = self.measure([(c, "PASS") for c in cases], timeout=1)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        summary, = json.loads(proc.stdout)
        self.assertEqual((summary["error"], summary["false_reject"], summary["correct_pass"]), (6, 0, 1))
        self.assertTrue(all(r["error"] and r["verdict"] is None for r in rows[:-1]))
        self.assertEqual(rows[0]["exit_code"], 7)
        self.assertEqual(rows[0]["error"], "provider exited 7: usage limit")
        self.assertEqual(rows[5]["answer"], "Maybe")
        self.assertEqual(rows[3]["error"], "deadline exceeded")
        self.assertIsNone(rows[-1]["usage"])


if __name__ == "__main__":
    unittest.main()
