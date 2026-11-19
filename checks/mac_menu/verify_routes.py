#!/usr/bin/env python3
"""Exercise the signed menu panel, both installed host readers and offline judge routing."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

from verify import Fixture, HERE, RegistryTests, installed_readers, running
from test_outside_judge import CONTRACT_TEXT, HANDOFF_TEXT, FAKE_CLI


def read_settings(fixture, project):
    repo, home = fixture.paths[project]
    results = {}
    for host, reader in fixture.readers.items():
        data = json.loads(subprocess.check_output(
            [sys.executable, "-B", reader, "--host", host, "--cwd", repo, "show"], env=fixture.env))
        assert Path(data["home"]) == home
        results[host] = data["settings"]
    assert results["claude"] == results["codex"]
    return results


def exercise_judge(fixture, host, tier, model, effort, skip=False):
    # An isolated PATH makes it impossible to call a paid provider in this test.
    folder = fixture.home / ("dispatch-" + uuid.uuid4().hex)
    folder.mkdir()
    (folder / "git").symlink_to(shutil.which("git"))
    for name in ("codex", "claude"):
        path = folder / name
        path.write_text("#!" + sys.executable + "\n" + FAKE_CLI)
        path.chmod(0o755)
    con, hnd, calls = (folder / name for name in ("contract.md", "handoff.md", "calls.jsonl"))
    con.write_text(CONTRACT_TEXT)
    hnd.write_text(HANDOFF_TEXT)
    env = dict(fixture.env, PATH=str(folder), FAKE_RECORD=str(calls), FAKE_MODE="success",
               FAKE_JSON_PATCH=json.dumps({"modelUsage": {model: {"inputTokens": 1}}}))
    for key in ("READBACK_JUDGE_ACTIVE", "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "ANTHROPIC_DEFAULT_FABLE_MODEL"):
        env.pop(key, None)
    runner = fixture.readers[host].with_name("outside_judge.py")
    args = [sys.executable, "-B", runner, "--host", host, "--tier", tier,
            "--lineage", "CON-20261119-" + str(30 + exercise_judge.calls), con, hnd, fixture.paths["sample-project"][0]]
    exercise_judge.calls += 1
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    if skip:
        assert "JUDGE      skipped" in result.stdout and not calls.exists()
        return "skipped"
    line, = calls.read_text().splitlines()
    call = json.loads(line)
    provider = "codex" if host == "claude" else "claude"
    assert call["provider"] == provider
    argv = call["args"]
    if provider == "codex":
        assert argv[argv.index("-m") + 1] == model and 'model_reasoning_effort=' + json.dumps(effort) in argv
    else:
        assert argv[argv.index("--model") + 1] == model and argv[argv.index("--effort") + 1] == effort
    return {"provider": provider, "model": model, "effort": effort}


exercise_judge.calls = 0


@contextmanager
def session_fixture(f):
    # Native metadata only. No transcript, prompt or fake provider response is read by the app.
    a, b = (str(uuid.uuid4()) for _ in range(2))
    claude = f.home / ".claude/sessions"
    claude.mkdir(parents=True)
    (claude / "99999999.json").write_text(json.dumps({
        "sessionId": b, "pid": 99999999, "cwd": str(f.paths["local-notes"][0]), "startedAt": 0
    }))
    codex = f.home / ".codex"
    codex.mkdir(exist_ok=True)
    with sqlite3.connect(codex / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, archived INTEGER, updated_at INTEGER)")
        db.execute("INSERT INTO threads VALUES (?, ?, 0, ?)", (a, str(f.paths["sample-project"][0]), int(time.time())))
    locks = codex / "thread-writer-locks"
    locks.mkdir()
    # The native writer holds an exclusive lock only while the thread is loaded.
    with (locks / (a + ".lock")).open("wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield {"sample-project": a, "local-notes": b}


def verify(bundle, out):
    base = RegistryTests()
    base.setUp()
    try:
        readers = installed_readers(None, None)
        f = Fixture(base, readers)
        binary = bundle / "Contents/MacOS" / plistlib.loads((bundle / "Contents/Info.plist").read_bytes())["CFBundleExecutable"]
        driver = f.home / "driver"
        subprocess.run(["xcrun", "swiftc", "-module-cache-path", os.environ.get("CLANG_MODULE_CACHE_PATH", str(f.home / "modules")),
                        HERE / "PanelDriver.swift", "-o", driver], check=True)
        observations = []
        project = "sample-project"
        other = f.settings("local-notes").read_bytes()
        with session_fixture(f) as sessions, running(binary, f.home, "Light") as pid:
            def ax(command, *args):
                return subprocess.check_output([driver, pid, command, *args], text=True, timeout=12).strip()
            def pick(field, option):
                if ax("value", field) == option:
                    return
                ax("press", field)
                ax("option", option)
                ax("menu-closed")
                assert ax("value", field) == option
            def set_route(host, tier, model, effort):
                pick("rb.host", {"claude":"Claude Code", "codex":"Codex"}[host])
                if tier == "step":
                    pick("rb.tier", "Her teslim")
                elif tier == "gate" and mode == "each":
                    pick("rb.tier", "Bölüm sonu")
                ax("set-text", "rb.model", model)
                pick("rb.effort", effort)
                before = read_settings(f, project)["claude"]
                ax("press", "rb.save")
                provider = "codex" if host == "claude" else "claude"
                expected = {"model": model, "effort": effort}
                before["judge"]["routes"][provider][tier] = expected
                deadline = time.monotonic() + 5
                while json.loads(f.settings(project).read_bytes())["judge"].get("routes", {}).get(
                        provider, {}).get(tier) != expected:
                    if time.monotonic() > deadline:
                        raise AssertionError(f"Failed to save {host}/{tier}: {ax('dump')}")
                    time.sleep(0.05)
                values = read_settings(f, project)
                assert all(v == before for v in values.values()), (host, tier, values)
                assert f.settings("local-notes").read_bytes() == other
                actual = exercise_judge(f, host, tier, model, effort)
                observations.append({"lead":host, "tier":tier, "mode":mode, "installed_readers":{
                    k:v["judge"]["routes"][provider][tier] for k,v in values.items()}, "offline_dispatch":actual})

            ax("press", "rb.status")
            pick("rb.project", project)
            ax("selected", "rb.mode.gate")
            mode = "gate"
            ax("press", "rb.routes")
            set_route("claude", "gate", "gpt-6.2-astra", "xhigh")
            set_route("codex", "gate", "claude-fable-5-2", "medium")
            for host in readers:
                assert exercise_judge(f, host, "step", "", "", skip=True) == "skipped"
            ax("press", "rb.mode.each")
            ax("selected", "rb.mode.each")
            if "rb.host" not in ax("dump"):
                ax("press", "rb.routes")
            mode = "each"
            set_route("claude", "step", "gpt-6.2-sol", "low")
            set_route("codex", "step", "opus", "high")
            for host, model, effort in (("claude","gpt-6.2-astra","xhigh"),("codex","claude-fable-5-2","medium")):
                exercise_judge(f, host, "gate", model, effort)
            (out/'routes.txt').write_text(ax("dump"))
            window = ax("window")
            subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-l", window, str(out/"routes.png")],check=True)
            ax("press", "rb.routes")
            ax("press", "rb.sessions")
            ax("wait", sessions[project])
            ax("wait", "Çalışıyor")
            ax("absent", sessions["local-notes"])
            (out/"running-session.txt").write_text(ax("dump"))
            pick("rb.project","local-notes")
            ax("selected","rb.mode.off")
            if sessions["local-notes"] not in ax("dump") and "rb.sessions.refresh" not in ax("dump"):
                ax("press","rb.sessions")
            ax("wait",sessions["local-notes"])
            ax("absent",sessions[project])
            ax("wait","Kayıtlı")
            ax("absent","rb.routes")
            (out/'sessions.txt').write_text(ax("dump"))
            assert f.settings("local-notes").read_bytes() == other
        return {"revision":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
                "installed_readers":{k:str(v) for k,v in readers.items()}, "edits":observations,
                "other_project_intact_sha256":hashlib.sha256(other).hexdigest(),
                "gate_mode_skipped_both_step_calls":True,"each_mode_retained_both_gate_routes":True,
                "project_session_isolation":True, "provider_transport":"offline fixture CLIs"}
    finally:
        base.doCleanups()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True,exist_ok=False)
    report = verify(args.bundle.resolve(strict=True), args.evidence)
    (args.evidence/"observations.json").write_text(json.dumps(report,indent=2)+"\n")
    print("Four route edits, two installed readers, both modes and project session isolation passed.")
