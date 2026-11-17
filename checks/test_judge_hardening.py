#!/usr/bin/env python3
"""Offline checks for the hardening of scripts/outside_judge.py.

Run one class: python3 -B checks/test_judge_hardening.py SignalTests
Run all:       python3 -B checks/test_judge_hardening.py

SignalTests   a SIGTERM to the helper interrupts the provider call (exit 3, group killed, output equal to
              the helper of commit bc63aae interrupted with SIGINT).
GuardTests    a failing repository lookup of the receipt cannot stop the helper; an interruption there
              still exits 3 before a provider starts.
BytecodeTests the helper writes no bytecode in the plugin layout and in the portable layout.
BaselineTests the portable copy equals its source; the oracle is the helper of bc63aae.

Output and error output are captured as bytes and compared as bytes. No model call runs. The run stops at
the first failure so that a mutation run is fast.
"""

import hashlib
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
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
HELPER = SCRIPTS / "outside_judge.py"
PORTABLE = ROOT / "portable" / "skills" / "readback" / "scripts"
BASELINE = "bc63aae"
REAL_GIT = shutil.which("git")

# One fake provider for every test. FAKE_MODE: pass | hang | hang_sigint.
# hang: consume the prompt, ignore SIGTERM, start a descendant in the same process group that ignores SIGTERM
# and appends to the heartbeat file, wait for the descendant's readiness, then never exit.
# hang_sigint: the same, and after the readiness send SIGINT to the parent (the reference run).
FAKE_CLI = r'''
import json, os, pathlib, signal, subprocess, sys, time
provider = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
prompt = sys.stdin.buffer.read()
with open(os.environ["FAKE_RECORD"], "a") as record:
    record.write(json.dumps({"provider": provider, "args": args, "prompt_len": len(prompt),
                             "prompt_tail": prompt[-20:].decode("utf-8", "replace")}) + "\n")
if not prompt.strip() or os.environ.get("READBACK_JUDGE_ACTIVE") != "1":
    sys.exit(96)
mode = os.environ.get("FAKE_MODE", "pass")
if mode in ("hang", "hang_sigint"):
    run = pathlib.Path(os.environ["FAKE_RUN"])
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (run / "provider.pid").write_text(str(os.getpid()))
    subprocess.Popen([sys.executable, "-c", os.environ["FAKE_DESCENDANT"], str(run)])
    while not (run / "ready").exists():
        time.sleep(0.02)
    if mode == "hang_sigint":
        os.kill(os.getppid(), signal.SIGINT)
    while True:
        time.sleep(1)
answer = os.environ["FAKE_ANSWER"]
if provider == "codex":
    pathlib.Path(args[args.index("-o") + 1]).write_bytes(answer.encode("utf-8"))
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": answer,
                      "modelUsage": json.loads(os.environ["FAKE_USAGE"])}))
'''

DESCENDANT = r'''
import os, pathlib, signal, sys, time
run = pathlib.Path(sys.argv[1])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
(run / "descendant.pid").write_text(str(os.getpid()))
fd = os.open(run / "heartbeat", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
os.write(fd, b"beat\n")
(run / "ready.tmp").write_text("1")
os.rename(run / "ready.tmp", run / "ready")
while True:
    os.write(fd, b".")
    time.sleep(0.05)
'''

PASS_ANSWER = "PASS Evidence supports the handoff.\nKeep this complete finding.\n"
CONTRACT_TEXT = ("CON-20261117-01  Do the thing.\nACC-1  The tool shall work.   Check: sh a.sh\n"
                 "Out of scope: everything else\n")
HANDOFF_TEXT = ("id        HND-20261117-01\nfrom      worker\ndate      2026-11-17\nstate     DONE\n"
                "readback  task: x | limits: y | done when: ACC-1\nsummary   Done.\nchanges   none\ncommit    none\n"
                "checks    sh a.sh | exit 0 | ok\ncontract  ACC-1 VERIFIED\nopen      -\nissues    -\nnext      Accept.\n")
HOSTS = (("claude", "codex"), ("codex", "claude"))  # (host, provider)
INTERRUPTED = b"receipt    none | interrupted"
GUARDED = b"receipt    none | cannot read the repository"
BUDGET = 10.0

GIT_IDENTITY = {
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}
_oracle_cache = {}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(repo, *args):
    env = clean_env()
    env.update(GIT_IDENTITY)
    result = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


def make_repo(parent, name="repo"):
    repo = Path(parent) / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("alpha\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def oracle_sources():
    """The helper and its rules as they were at bc63aae."""
    if not _oracle_cache:
        for name, path in (("helper", "scripts/outside_judge.py"), ("rules", "agents/judge.md")):
            result = subprocess.run(["git", "-C", str(ROOT), "show", BASELINE + ":" + path], env=clean_env(),
                                    capture_output=True, check=True, timeout=30)
            _oracle_cache[name] = result.stdout
    return _oracle_cache


def default_sigint():
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # a helper started from a background shell would ignore SIGINT


def new_world(base, bytecode=False):
    """Fake providers, the real git behind a symlink, python3, the oracle helper and an environment."""
    base = Path(base)
    for name in ("bin", "gitbin", "pybin", "tmp", "runs"):
        (base / name).mkdir()
    (base / "gitbin" / "git").symlink_to(REAL_GIT)
    (base / "pybin" / "python3").symlink_to(os.path.realpath(sys.executable))
    for provider in ("claude", "codex"):
        fake = base / "bin" / provider
        fake.write_text("#!" + sys.executable + "\n" + FAKE_CLI)
        fake.chmod(0o755)
    (base / "oracle").mkdir()
    sources = oracle_sources()
    (base / "oracle" / "outside_judge.py").write_bytes(sources["helper"])
    (base / "oracle" / "judge.md").write_bytes(sources["rules"])
    env = {
        "PATH": os.pathsep.join(str(base / name) for name in ("bin", "gitbin", "pybin")),
        "TMPDIR": str(base / "tmp"), "READBACK_TEAMMATES": str(base / "TEAMMATES.md"),
        "READBACK_JUDGE_SECONDS": "30", "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8",
        "FAKE_ANSWER": PASS_ANSWER, "FAKE_USAGE": json.dumps({"claude-fable-5-2": {"inputTokens": 10}}),
        "FAKE_DESCENDANT": DESCENDANT, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }
    if not bytecode:
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    return types.SimpleNamespace(base=base, env=env, counter=[0], pids=[])


def fresh_dir(world, label):
    world.counter[0] += 1
    folder = world.base / ("%s-%d" % (label, world.counter[0]))
    folder.mkdir()
    return folder


def make_case(world, host):
    folder = fresh_dir(world, "case")
    real = folder / "inputs"
    real.mkdir()
    (real / "contract.md").write_bytes(CONTRACT_TEXT.encode("utf-8"))
    (real / "handoff.md").write_bytes(HANDOFF_TEXT.encode("utf-8"))
    repo = make_repo(folder, "repo")
    return types.SimpleNamespace(host=host, repo=repo, contract=real / "contract.md", handoff=real / "handoff.md",
                                 common=repo / ".git")


def spawn(world, argv, case, mode, extra_env=None):
    """Start a helper process. Output and error output are pipes of bytes. Return (process, run folder, record)."""
    run = fresh_dir(world, "run")
    record = run / "calls.jsonl"
    env = dict(world.env)
    env.update({"FAKE_RECORD": str(record), "FAKE_MODE": mode, "FAKE_RUN": str(run)})
    env.update(extra_env or {})
    process = subprocess.Popen([*argv, "--host", case.host, str(case.contract), str(case.handoff), str(case.repo)],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=default_sigint)
    return process, run, record


def calls_of(record):
    return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []


def finish(process, run, record, timeout=90):
    out, err = process.communicate(timeout=timeout)
    return types.SimpleNamespace(code=process.returncode, out=out, err=err, calls=calls_of(record), run=run)


def oracle_argv(world):
    return [sys.executable, "-B", str(world.base / "oracle" / "outside_judge.py")]


def split_receipt(out):
    """Return (the output bytes without their third line, the third line as bytes). Split at b"\\n" only."""
    first, second, third, rest = out.split(b"\n", 3)
    return first + b"\n" + second + b"\n" + rest, third


def compare_with_oracle(test, old, new, third_line):
    """Bytes: the error output and the output without the third line equal those of the oracle. The statuses
    are integers and equal. The third line is exactly third_line."""
    test.assertIsInstance(new.code, int)
    test.assertIsInstance(old.code, int)
    test.assertEqual(new.err, old.err)
    test.assertEqual(new.code, old.code)
    rest, third = split_receipt(new.out)
    test.assertEqual(third, third_line)
    test.assertEqual(rest, old.out)
    test.assertEqual(new.out.count(b"\nreceipt    "), 1)
    test.assertNotIn(b"receipt    ", old.out)


# ------------------------------------------------------ process observations

def process_state(pid):
    """None when the PID does not exist, else the `ps` state letters of the process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, timeout=10).stdout
    return out.decode("ascii", "replace").strip() or None


def pid_gone(pid):
    return process_state(pid) is None


def pid_ended(pid):
    """The process is gone or a zombie."""
    state = process_state(pid)
    return state is None or state.startswith("Z")


def poll(condition, seconds=BUDGET):
    end = time.monotonic() + seconds
    while True:
        if condition():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def heartbeat_quiet(path, gap=0.3):
    """The file has the same bytes at two observations at least `gap` seconds apart."""
    first = Path(path).read_bytes()
    began = time.monotonic()
    while time.monotonic() - began < gap:
        time.sleep(0.02)
    return Path(path).read_bytes() == first


def read_pid(path):
    return int(Path(path).read_text())


def readback_files(common):
    """{relative path: sha256} of every file below <common>/readback."""
    found = {}
    for dirpath, _names, files in os.walk(Path(common) / "readback"):
        for name in files:
            path = Path(dirpath) / name
            found[str(path.relative_to(Path(common) / "readback"))] = sha(path.read_bytes())
    return found


def start_descendant(world):
    run = fresh_dir(world, "desc")
    process = subprocess.Popen([sys.executable, "-c", DESCENDANT, str(run)])
    world.pids.append(process.pid)
    return process, run


class WorldCase(unittest.TestCase):
    bytecode = False

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="readback-hardening-")
        cls.world = new_world(Path(os.path.realpath(cls.temp.name)), cls.bytecode)

    @classmethod
    def tearDownClass(cls):
        for pid in cls.world.pids:  # leftovers of a failed test
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for pidfile in cls.world.base.glob("run-*/*.pid"):
            try:
                os.killpg(int(pidfile.read_text()), signal.SIGKILL) if pidfile.name == "provider.pid" else \
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, ValueError):
                pass
        cls.temp.cleanup()


# ------------------------------------------------------------------ SignalTests

class SignalTests(WorldCase):
    def reference(self, case):
        """The helper of bc63aae, interrupted with SIGINT by its provider after the same readiness."""
        process, run, record = spawn(self.world, oracle_argv(self.world), case, "hang_sigint")
        old = finish(process, run, record)
        self.assertEqual(old.code, 3)
        self.assertTrue((run / "ready").exists())
        self.assertIn(b"no verdict: the judge was interrupted", old.out)
        self.assertTrue(poll(lambda: pid_gone(read_pid(run / "provider.pid"))))
        self.assertTrue(poll(lambda: pid_ended(read_pid(run / "descendant.pid"))))
        return old

    def test_the_comparison_with_the_oracle_can_fail_on_one_byte(self):
        def run(out, err=b"", code=3):
            return types.SimpleNamespace(out=out, err=err, code=code)
        line = INTERRUPTED + b"\n"
        old = run(b"JUDGE      none | x\nexecution  y\n")
        good = run(b"JUDGE      none | x\nexecution  y\n" + line)
        compare_with_oracle(self, old, good, INTERRUPTED)
        bad_outputs = (
            run(b"JUDGE      none | x\r\nexecution  y\n" + line),
            run(b"JUDGE      none | x\nexecution  z\n" + line),
            run(b"JUDGE      none | x\nexecution  y\n" + line + b"\n"),
            run(b"JUDGE      none | x\nexecution  y\n" + line, err=b"warning\n"),
            run(b"JUDGE      none | x\nexecution  y\n" + line, code=2),
            run(b"JUDGE      none | x\nexecution  y\nreceipt    none | interrupte\n"),
            run(b"JUDGE      none | x\nexecution  y\nreceipt    none | interrupted \n"),
        )
        for number, bad in enumerate(bad_outputs):
            with self.subTest(case=number):
                with self.assertRaises(AssertionError):
                    compare_with_oracle(self, old, bad, INTERRUPTED)
        with self.assertRaises(AssertionError):
            compare_with_oracle(self, run(b"a\nb\n", b"w\n"), run(b"a\nb\n" + line, b"w\r\n"), INTERRUPTED)
        with self.assertRaises(AssertionError):
            compare_with_oracle(self, run(b"a\nb\n", code=3), run(b"a\nb\n" + line, code=True), INTERRUPTED)

    def test_the_process_observations_can_fail(self):
        process, run = start_descendant(self.world)
        self.assertTrue(poll(lambda: (run / "ready").exists()))
        pid = process.pid
        self.assertEqual(read_pid(run / "descendant.pid"), pid)
        self.assertFalse(heartbeat_quiet(run / "heartbeat"))  # a live descendant keeps writing
        self.assertFalse(pid_gone(pid))
        self.assertFalse(pid_ended(pid))
        process.kill()
        self.assertTrue(poll(lambda: pid_ended(pid)))  # killed, not yet reaped: a zombie counts as ended
        self.assertTrue(process_state(pid).startswith("Z"))  # the zombie still has a PID
        process.wait()
        self.assertTrue(pid_gone(pid))
        self.assertTrue(heartbeat_quiet(run / "heartbeat"))
        self.assertFalse(pid_gone(os.getpid()))

    def test_sigterm_interrupts_the_provider_call(self):
        for host, provider in HOSTS:
            with self.subTest(provider=provider):
                case = make_case(self.world, host)
                old = self.reference(case)
                before = readback_files(case.common)
                self.assertEqual(before, {})
                process, run, record = spawn(self.world, [sys.executable, "-B", str(HELPER)], case, "hang")
                self.world.pids.append(process.pid)
                ready = poll(lambda: (run / "ready").exists() or process.poll() is not None, 30)
                self.assertTrue(ready)
                self.assertIsNone(process.poll(), "the helper ended before readiness")
                self.assertTrue((run / "ready").exists())
                provider_pid = read_pid(run / "provider.pid")
                descendant_pid = read_pid(run / "descendant.pid")
                self.assertNotEqual(provider_pid, descendant_pid)
                self.assertFalse(pid_ended(provider_pid))
                self.assertFalse(pid_ended(descendant_pid))
                self.assertGreater(len((run / "heartbeat").read_bytes()), 0)
                self.assertEqual(len(calls_of(record)), 1)
                self.assertEqual(calls_of(record)[0]["provider"], provider)
                self.assertEqual(calls_of(record)[0]["prompt_tail"], "--- END RECORDS ---\n")
                began = time.monotonic()
                process.send_signal(signal.SIGTERM)
                try:
                    out, err = process.communicate(timeout=BUDGET)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    self.fail("the helper did not exit within %s seconds of SIGTERM" % BUDGET)
                elapsed = time.monotonic() - began
                self.assertEqual(process.returncode, 3)
                self.assertLessEqual(elapsed, BUDGET)
                new = types.SimpleNamespace(code=process.returncode, out=out, err=err)
                compare_with_oracle(self, old, new, INTERRUPTED)
                self.assertIn(b"no verdict: the judge was interrupted", new.out)
                files = readback_files(case.common)
                self.assertEqual(len(files), 1, files)
                (name,) = files
                self.assertRegex(name, r"^reviews/[0-9a-f]{64}/[0-9a-f]{32}\.started\.json$")
                started = json.loads((case.common / "readback" / name).read_text())
                self.assertEqual(started["attempt_id"], Path(name).name[:-len(".started.json")])
                self.assertEqual(started["route"]["provider"], provider)
                self.assertFalse(any(n.endswith(".completed.json") for n in files))
                self.assertFalse((case.common / "readback" / "by-manifest").exists())
                self.assertTrue(poll(lambda: pid_gone(provider_pid)), "the provider PID still exists")
                self.assertTrue(poll(lambda: pid_ended(descendant_pid)), "the descendant is still running")
                self.assertTrue(heartbeat_quiet(run / "heartbeat"))
                self.assertGreater(len((run / "heartbeat").read_bytes()), 0)


# ------------------------------------------------------------------ GuardTests

DRIVER = r'''
import json, os, sys
spec = json.loads(os.environ["FAULT_SPEC"])
sys.path.insert(0, spec["scripts"])
import review_capture, review_receipt
log = open(spec["log"], "a")


def note(*item):
    log.write(json.dumps(item) + "\n")
    log.flush()


real_capture = review_capture.capture


def capture(repo, seconds):
    note("capture", os.fspath(repo))
    return real_capture(repo, seconds)


review_capture.capture = capture


def wrap(module, name):
    real = getattr(module, name)

    def inner(*args, **kwargs):
        note("lookup", name, os.fspath(args[0]))
        mode = spec["lookup"].get(name)
        if mode == "RuntimeError":
            raise RuntimeError("injected")
        if mode == "KeyboardInterrupt":
            raise KeyboardInterrupt()
        if mode == "InterruptedError":
            raise InterruptedError("injected")
        return real(*args, **kwargs)
    setattr(module, name, inner)


wrap(review_receipt, "repository_info")
wrap(review_capture, "git_common_dir")
import outside_judge
sys.exit(outside_judge.main())
'''

LOOKUPS = ("repository_info", "git_common_dir")


class GuardTests(WorldCase):
    def driven(self, case, lookup, mode):
        """Run the helper through the driver, which injects `mode` into `lookup` (None: no injection)."""
        folder = fresh_dir(self.world, "driver")
        (folder / "driver.py").write_text(DRIVER)
        log = folder / "log.jsonl"
        spec = {"scripts": str(SCRIPTS), "log": str(log), "lookup": {lookup: mode} if mode else {}}
        process, run, record = spawn(self.world, [sys.executable, "-B", str(folder / "driver.py")], case, "pass",
                                     {"FAULT_SPEC": json.dumps(spec)})
        new = finish(process, run, record)
        new.log = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return new

    def common_dir(self, case):
        return Path(git(case.repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())

    def oracle(self, case, mode):
        process, run, record = spawn(self.world, oracle_argv(self.world), case, mode)
        return finish(process, run, record)

    def test_the_comparison_with_the_oracle_can_fail_on_one_byte(self):
        def run(out, err=b"", code=0):
            return types.SimpleNamespace(out=out, err=err, code=code)
        old = run(b"JUDGE      PASS | x\nexecution  y\n\nPASS a\nb\n")
        line = GUARDED + b"\n"
        good = run(b"JUDGE      PASS | x\nexecution  y\n" + line + b"\nPASS a\nb\n")
        compare_with_oracle(self, old, good, GUARDED)
        bad_outputs = (
            run(b"JUDGE      PASS | x\nexecution  y\n" + line + b"\nPASS a\r\nb\n"),
            run(b"JUDGE      PASS | x\nexecution  y\n" + line + b"\nPASS a\nb\r"),
            run(b"JUDGE      PASS | x\nexecution  y\n" + line + b"\nPASS a\nb\n", err=b"warning\r"),
            run(b"JUDGE      PASS | x\nexecution  y\n" + line + b"\nPASS a\nb\n", code=3),
            run(b"JUDGE      PASS | x\nexecution  y\nreceipt    none | cannot read the repositor\n\nPASS a\nb\n"),
            run(b"JUDGE      PASS | x\nexecution  y\nreceipt    none | cannot write the record\n\nPASS a\nb\n"),
        )
        for number, bad in enumerate(bad_outputs):
            with self.subTest(case=number):
                with self.assertRaises(AssertionError):
                    compare_with_oracle(self, old, bad, GUARDED)

    def test_the_driver_passes_the_lookups_through_without_an_injection(self):
        for host, provider in HOSTS:
            with self.subTest(provider=provider):
                case = make_case(self.world, host)
                new = self.driven(case, "repository_info", None)
                self.assertEqual(new.code, 0)
                self.assertEqual([e[1] for e in new.log if e[0] == "lookup"], list(LOOKUPS))
                self.assertEqual(len([e for e in new.log if e[0] == "capture"]), 2)
                third = split_receipt(new.out)[1]
                self.assertRegex(third.decode("utf-8"),
                                 r"^receipt    [0-9a-f]{32} \| ends-equal \| validation true \| .+$")
                self.assertTrue((self.common_dir(case) / "readback").is_dir())

    def test_a_failing_lookup_cannot_stop_the_helper(self):
        for host, provider in HOSTS:
            for lookup in LOOKUPS:
                with self.subTest(provider=provider, lookup=lookup):
                    case = make_case(self.world, host)
                    old = self.oracle(case, "pass")
                    self.assertEqual((old.code, len(old.calls)), (0, 1))
                    new = self.driven(case, lookup, "RuntimeError")
                    reached = [e for e in new.log if e[0] == "lookup" and e[1] == lookup]
                    self.assertEqual(len(reached), 1)
                    self.assertEqual(reached[0][2], os.path.realpath(case.repo))
                    compare_with_oracle(self, old, new, GUARDED)
                    self.assertEqual(new.code, 0)
                    self.assertEqual(len(new.calls), 1)
                    self.assertEqual(new.calls[0]["provider"], provider)
                    self.assertFalse((self.common_dir(case) / "readback").exists())
                    self.assertEqual([e for e in new.log if e[0] == "capture"], [])

    def test_an_interruption_at_a_lookup_exits_3_before_a_provider_starts(self):
        for host, provider in HOSTS:
            for lookup in LOOKUPS:
                for mode in ("KeyboardInterrupt", "InterruptedError"):
                    with self.subTest(provider=provider, lookup=lookup, mode=mode):
                        case = make_case(self.world, host)
                        old = self.oracle(case, "hang_sigint")
                        self.assertEqual(old.code, 3)
                        new = self.driven(case, lookup, mode)
                        self.assertEqual(len([e for e in new.log if e[0] == "lookup" and e[1] == lookup]), 1)
                        compare_with_oracle(self, old, new, INTERRUPTED)
                        self.assertEqual(new.code, 3)
                        self.assertIn(b"no verdict: the judge was interrupted", new.out)
                        self.assertEqual(new.calls, [])
                        self.assertEqual([e for e in new.log if e[0] == "capture"], [])
                        self.assertFalse((self.common_dir(case) / "readback").exists())


# ---------------------------------------------------------------- BytecodeTests

PLUGIN_FILES = ("scripts/outside-judge.sh", "scripts/outside_judge.py", "scripts/review_capture.py",
                "scripts/review_receipt.py", "scripts/validate_handoff.py", "agents/judge.md")
RECEIPT_RE = r"^receipt    [0-9a-f]{32} \| ends-equal \| validation true \| .+$"


def bytecode_below(folder):
    found = []
    for dirpath, names, files in os.walk(folder):
        found.extend(os.path.join(dirpath, n) for n in names if n == "__pycache__")
        found.extend(os.path.join(dirpath, n) for n in files if n.endswith(".pyc"))
    return found


class BytecodeTests(WorldCase):
    bytecode = True

    def layout(self, kind):
        """A fresh writable copy of the files of the working tree. Return (copy folder, command)."""
        copy = fresh_dir(self.world, kind)
        if kind == "plugin":
            for name in PLUGIN_FILES:
                (copy / name).parent.mkdir(exist_ok=True)
                shutil.copyfile(ROOT / name, copy / name)
                (copy / name).chmod(0o755 if name.endswith(".sh") else 0o644)
            return copy, ["/bin/bash", str(copy / "scripts" / "outside-judge.sh")]
        for source in sorted(PORTABLE.iterdir()):
            if source.is_file() and not source.name.endswith(".pyc"):
                shutil.copyfile(source, copy / source.name)
                (copy / source.name).chmod(0o644)
        return copy, [str(self.world.base / "pybin" / "python3"), str(copy / "outside_judge.py")]

    def test_the_helper_writes_no_bytecode_in_either_layout(self):
        for name in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"):
            self.assertNotIn(name, self.world.env)
        for kind in ("plugin", "portable"):
            for host, provider in HOSTS:
                with self.subTest(layout=kind, provider=provider):
                    copy, command = self.layout(kind)
                    self.assertEqual(bytecode_below(copy), [])
                    case = make_case(self.world, host)
                    process, run, record = spawn(self.world, command, case, "pass")
                    new = finish(process, run, record)
                    self.assertEqual(new.err, b"")
                    self.assertEqual(new.code, 0)
                    self.assertTrue(new.out.startswith(b"JUDGE      PASS | " + provider.encode() + b":"), new.out)
                    self.assertEqual(len(new.calls), 1)
                    third = split_receipt(new.out)[1].decode("utf-8")
                    self.assertRegex(third, RECEIPT_RE)
                    self.assertEqual(bytecode_below(copy), [])

    def test_the_check_can_fail_when_bytecode_is_written(self):
        copy, _command = self.layout("plugin")
        self.assertEqual(bytecode_below(copy), [])
        env = {k: v for k, v in self.world.env.items()}
        result = subprocess.run([sys.executable, "-c", "import review_receipt"], cwd=copy / "scripts", env=env,
                                capture_output=True, timeout=30, check=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(bytecode_below(copy)), 2)  # the folder and one .pyc file
        with self.assertRaises(AssertionError):
            self.assertEqual(bytecode_below(copy), [])


# ---------------------------------------------------------------- BaselineTests

class BaselineTests(unittest.TestCase):
    def test_the_comparison_of_files_can_fail_on_one_byte(self):
        with tempfile.TemporaryDirectory(prefix="readback-baseline-") as temp:
            one, two = Path(temp) / "one", Path(temp) / "two"
            one.write_bytes(b"line\n")
            two.write_bytes(b"line\n")
            self.assertEqual(one.read_bytes(), two.read_bytes())
            two.write_bytes(b"line\r")
            with self.assertRaises(AssertionError):
                self.assertEqual(one.read_bytes(), two.read_bytes())

    def test_the_portable_copy_equals_its_source_in_bytes(self):
        source = (SCRIPTS / "outside_judge.py").read_bytes()
        copy = (PORTABLE / "outside_judge.py").read_bytes()
        self.assertGreater(len(source), 0)
        self.assertEqual(sha(copy), sha(source))
        self.assertEqual(copy, source)

    def test_sync_check_exits_0(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "sync.py"), "--check"], capture_output=True,
                                timeout=60, cwd=ROOT)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, b"portable skills are current\n", b""))

    def test_the_oracle_is_the_helper_of_bc63aae_without_a_receipt_line(self):
        helper = oracle_sources()["helper"]
        self.assertNotIn(b"receipt", helper)
        self.assertIn(b"def run_process", helper)
        self.assertIn(b"receipt", (SCRIPTS / "outside_judge.py").read_bytes())


if __name__ == "__main__":
    unittest.main(failfast=True)
