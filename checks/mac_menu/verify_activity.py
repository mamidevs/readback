#!/usr/bin/env python3
"""Open both providers' sessions in the signed panel and observe a live event without a model call."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess

from verify import Fixture, HERE, RegistryTests, running
from verify_routes import session_fixture


def append(path, event):
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    with path.open("a") as stream:
        stream.write(json.dumps(event) + "\n")


def verify(bundle, output):
    base = RegistryTests()
    base.setUp()
    try:
        fixture = Fixture(base, {})
        driver = fixture.home / "panel-driver"
        cache = os.environ.get("CLANG_MODULE_CACHE_PATH", str(fixture.home / "modules"))
        subprocess.run(["xcrun", "swiftc", "-module-cache-path", cache, HERE / "PanelDriver.swift",
                        "-o", driver], check=True)
        with session_fixture(fixture) as rows:
            codex = rows["sample-project"]
            claude = rows["local-notes"]
            cwd = str(fixture.paths["sample-project"][0])
            local = str(fixture.paths["local-notes"][0])
            rollout = fixture.home / ".codex/sessions/2026/01/01" / f"rollout-2026-01-01T12-00-00-{codex}.jsonl"
            rollout.parent.mkdir(parents=True)
            append(rollout, {"type": "session_meta", "payload": {"id": codex, "cwd": cwd}})
            append(rollout, {"type": "turn_context", "payload": {"model": "gpt-6.2-sol"}})
            append(rollout, {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                       "arguments": json.dumps({"cmd": "pytest -q"})}})
            with sqlite3.connect(fixture.home / ".codex/state_5.sqlite") as db:
                db.execute("ALTER TABLE threads ADD COLUMN rollout_path TEXT")
                db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(rollout), codex))
            transcript = fixture.home / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", local) / (claude + ".jsonl")
            transcript.parent.mkdir(parents=True)
            append(transcript, {"type": "assistant", "sessionId": claude, "cwd": local,
                  "message": {"model": "claude-sonnet-4-6", "content": [{"type": "tool_use", "name": "Read",
                  "input": {"file_path": "fixture-private-filename"}}]}})
            with running(bundle / "Contents/MacOS/Readback", fixture.home, "Light") as pid:
                def ax(action, *args):
                    return subprocess.check_output([driver, pid, action, *args], text=True, timeout=12).strip()

                def select(project):
                    if ax("value", "rb.project") != project:
                        ax("press", "rb.project")
                        ax("option", project)
                        ax("menu-closed")

                def capture(name):
                    subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-l", ax("window"),
                                    str(output / (name + ".png"))], check=True, timeout=10)

                ax("press", "rb.status")
                select("sample-project")
                for mode in ("gate", "each", "off", "gate"):
                    if ax("value", "rb.mode." + mode) != "Seçili":
                        ax("press", "rb.mode." + mode)
                    ax("selected", "rb.mode." + mode)
                    print(ax("mode-fit"))
                capture("mode-labels")
                ax("press", "rb.sessions")
                ax("press", "rb.session.codex." + codex)
                ax("wait", "rb.activity.openCodex")
                ax("wait", "gpt-6.2-sol")
                ax("wait", "Test çalıştırma isteği")
                append(rollout, {"type": "event_msg", "payload": {"type": "task_complete"}})
                ax("wait", "İşlem turu tamamlandı")
                capture("session-activity")
                select("local-notes")
                ax("absent", "rb.activity.back")
                if "rb.sessions.refresh" not in ax("dump"):
                    ax("press", "rb.sessions")
                ax("press", "rb.session.claude." + claude)
                ax("wait", "claude-sonnet-4-6")
                ax("wait", "Dosya okuma isteği")
                ax("absent", "rb.activity.openCodex")
                dump = ax("dump")
                assert codex not in dump and "fixture-private-filename" not in dump
                ax("press", "rb.activity.back")
                ax("wait", "rb.session.claude." + claude)
                ax("absent", "rb.activity.back")
        print("Mode labels fit. Both sessions opened. Appended activity appeared automatically. Project switch and back passed.")
    finally:
        base.doCleanups()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    verify(args.bundle.resolve(strict=True), args.output)
