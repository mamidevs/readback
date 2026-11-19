"""Offline CLI integration tests. A local stub supplies Codex events and answers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENTRY = Path(__file__).resolve().with_name("outside-judge.sh")
FAKE = r'''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
assert "--ephemeral" in args and "--ignore-user-config" in args and "--ignore-rules" in args
assert args[args.index("-s")+1] == "read-only" and args[-1] == "-"
assert all(flag in args for flag in ("skip_host_skill_discovery", "shell_tool", "multi_agent", "apps", "plugins", "hooks"))
assert not list(Path.cwd().iterdir())
prompt = sys.stdin.buffer.read().decode()
assert "blind-label" not in prompt and "answer-key.json" not in prompt
case = prompt.splitlines()[-1]
model = args[args.index("-m")+1]
if case == "sleep":
    time.sleep(20)
if case == "exit":
    print("usage limit for excluded-account-id", file=sys.stderr)
    sys.exit(7)
if case == "tool":
    print(json.dumps({"type":"item.completed", "item":{"type":"command_execution"}}))
if case == "malformed":
    print("bad-json")
answer = ("PASS" if model == "alpha" else "FAIL") + "  final answer.\r\n"
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
