#!/usr/bin/env python3
"""Offline checks for scripts/trusted_run.py (CON-20261123-11).

Run: python3 -m unittest discover -s checks -p 'test_trusted_run.py' -v
"""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "trusted_run.py"
SENTENCE = "`scripts/trusted_run.py` is such a script for checks that are shell commands."
BEFORE = "Use a lead-controlled script or project CI on a clean checkout with fresh HOME and TMPDIR."


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def contract(*checks):
    lines = ["CON-20261123-99  Test contract."]
    for n, check in enumerate(checks, 1):
        lines.append(f"ACC-{n}  Statement {n}." + (f"  Check: {check}" if check else ""))
    return "\n".join(lines) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trusted-run-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "a.txt").write_text("a\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
        self.out = self.tmp / "out"

    def run_script(self, *args, repo=True):
        cmd = [sys.executable, str(SCRIPT)] + (["--repo", str(self.repo)] if repo else []) + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    def run_commands(self, *commands, extra=()):
        return self.run_script("--commit", "HEAD", "--out", str(self.out), *extra, "--", *commands)

    def report(self):
        return json.loads((self.out / "report.json").read_text())


class RunTests(Base):
    def test_runs_in_a_detached_checkout_with_fresh_home_and_writes_the_report(self):
        done = self.run_commands("git rev-parse --abbrev-ref HEAD; pwd; echo $HOME; echo $TMPDIR")
        self.assertEqual(done.returncode, 0, done.stderr)
        report = self.report()
        self.assertEqual(json.loads(done.stdout), report)
        self.assertEqual(report["commit"], git(self.repo, "rev-parse", "HEAD").strip())
        self.assertEqual(report["tree_changes_after_run"], [])
        result = report["results"][0]
        for key in ("command", "exit", "seconds", "output", "sha256", "tail"):
            self.assertIn(key, result)
        text = Path(result["output"]).read_text().split("\n")
        self.assertEqual(text[0], "HEAD")
        self.assertNotEqual(text[1], str(self.repo))
        self.assertTrue(text[2].startswith(str(self.out)) and text[3].startswith(str(self.out)), text)

    def test_exit_is_1_when_a_command_fails_and_the_chain_uses_sh_ec(self):
        done = self.run_commands("true", "false; true")
        self.assertEqual(done.returncode, 1)
        self.assertEqual([r["exit"] for r in self.report()["results"]], [0, 1])

    def test_a_malformed_argument_gives_exit_2_one_stderr_line_and_no_stdout(self):
        for args in (["--out", str(self.out), "--", "true"], ["--commit"], ["--commit", "HEAD", "--out", str(self.out), "--nope"],
                     ["--commit", "no-such-rev", "--out", str(self.out), "--", "true"]):
            with self.subTest(args=args):
                done = self.run_script(*args)
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)

    def test_without_repo_it_uses_the_repository_that_holds_the_script(self):
        (self.repo / "scripts").mkdir()
        shutil.copy(SCRIPT, self.repo / "scripts" / "trusted_run.py")
        git(self.repo, "add", "scripts")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "two")
        done = subprocess.run([sys.executable, str(self.repo / "scripts" / "trusted_run.py"), "--commit", "HEAD",
                               "--out", str(self.out), "--", "true"], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.report()["repository"], str(self.repo))


class ContractTests(Base):
    def write(self, name, text):
        path = self.tmp / name
        path.write_text(text)
        return str(path)

    def test_contract_checks_run_once_each_in_order_before_the_commands(self):
        one = self.write("one.md", contract("echo a", "echo b", "echo a"))
        two = self.write("two.md", contract("echo c", "echo b"))
        done = self.run_commands("echo z", extra=("--contract", one, "--contract", two))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([r["command"] for r in self.report()["results"]], ["echo a", "echo b", "echo c", "echo z"])

    def test_nothing_runs_and_exit_is_2_without_a_check_or_without_a_command(self):
        cases = {"no check": ["--contract", self.write("n.md", contract("echo a", None))],
                 "nothing left": ["--contract", self.write("e.md", "CON-20261123-99  Empty.\n")]}
        for name, extra in cases.items():
            with self.subTest(name):
                done = self.run_script("--commit", "HEAD", "--out", str(self.out), *extra)
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertFalse(self.out.exists())
        self.assertEqual(self.run_script("--commit", "HEAD", "--out", str(self.out)).returncode, 2)


class CleanupTests(Base):
    def worktrees(self):
        return git(self.repo, "worktree", "list", "--porcelain").count("worktree ")

    def test_checkout_is_gone_and_tree_unchanged_after_a_normal_run(self):
        done = self.run_commands("echo x > made.txt")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.worktrees(), 1)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(self.report()["tree_changes_after_run"], ["?? made.txt"])

    def test_checkout_and_process_are_gone_after_sigterm_or_sigint(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(sig=sig.name):
                out = self.tmp / f"out-{sig.name}"
                proc = subprocess.Popen([sys.executable, str(SCRIPT), "--repo", str(self.repo), "--commit", "HEAD",
                                         "--out", str(out), "--", "echo $$ > ../pid; exec sleep 60"],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.addCleanup(proc.kill)
                deadline = time.time() + 10
                while not (out / "pid").exists() and time.time() < deadline:
                    time.sleep(0.05)
                time.sleep(0.2)
                pid = int((out / "pid").read_text())
                proc.send_signal(sig)
                proc.communicate(timeout=10)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(self.worktrees(), 1)
                self.assertFalse((out / "checkout").exists())
                self.assertEqual(git(self.repo, "status", "--porcelain"), "")
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)


class TextTests(unittest.TestCase):
    def test_the_sentence_stands_once_directly_after_its_line_in_the_style_and_protocol(self):
        for rel in ("output-styles/readback.md", "PROTOCOL.md", "portable/skills/readback/SKILL.md"):
            with self.subTest(rel):
                lines = (ROOT / rel).read_text(encoding="utf-8").split("\n")
                self.assertEqual(lines.count(SENTENCE), 1)
                self.assertEqual(lines.index(SENTENCE), lines.index(BEFORE) + 1)

    def test_sync_copies_the_script_into_the_portable_lead_skill(self):
        copy = ROOT / "portable" / "skills" / "readback" / "scripts" / "trusted_run.py"
        self.assertEqual(copy.read_text(encoding="utf-8"), SCRIPT.read_text(encoding="utf-8"))
        self.assertIn("trusted_run.py", (ROOT / "scripts" / "sync.py").read_text(encoding="utf-8"))

    def test_amendment_17_has_its_title_and_names_the_script(self):
        text = (ROOT / "AMENDMENT-17.md").read_text(encoding="utf-8")
        self.assertEqual(text.split("\n")[0], "# Amendment 17")
        self.assertIn("scripts/trusted_run.py", text)


if __name__ == "__main__":
    unittest.main()
