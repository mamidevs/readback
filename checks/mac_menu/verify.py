#!/usr/bin/env python3
"""Drive the real macOS status item. Requires Accessibility and a logged-in desktop.

Usage: python3 -B checks/mac_menu/verify.py /path/to/Readback.app /new/evidence-dir
No provider calls. All project homes and application preferences are disposable.
"""
from contextlib import contextmanager
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from test_project_registry import RegistryTests, write_json


def installed_readers(claude, codex):
    if not claude:
        index = Path.home() / ".claude/plugins/installed_plugins.json"
        entries = json.loads(index.read_text())["plugins"]["readback@readback-local"]
        root = next(item["installPath"] for item in entries if item["scope"] == "user")
        claude = Path(root) / "scripts/readback_settings.py"
    codex = codex or (Path.home() / ".agents/skills/readback/scripts/readback_settings.py")
    readers = {"claude": claude.resolve(strict=True), "codex": codex.resolve(strict=True)}
    return readers


class Fixture:
    def __init__(self, base, readers):
        self.home = base.base
        self.book = base.logbook
        self.env = dict(os.environ, HOME=str(self.home), CFFIXED_USER_HOME=str(self.home),
                        PYTHONDONTWRITEBYTECODE="1")
        base.activate()
        self.paths = {"sample-project": (base.repo, base.home),
                      "local-notes": (self.home / "repositories/local-notes", self.book / "local-notes")}
        base.git_init(self.paths["local-notes"][0])
        self.paths["local-notes"][1].mkdir()
        (self.paths["local-notes"][1] / "CURRENT.md").write_text("CURRENT   local-notes\n")
        self.readers = readers
        for name, mode in [("sample-project", "gate"), ("local-notes", "off")]:
            write_json(self.settings(name), {"schema_version": 1, "judge": {
                "mode": mode, "deadline_seconds": {"gate": 725, "step": 345}}, "commit_check": "observe"})

    def settings(self, name):
        return self.paths[name][1] / "settings.json"

    def observed(self, name):
        repo, home = self.paths[name]
        result = {}
        for host, script in self.readers.items():
            answer = subprocess.check_output([sys.executable, "-B", script, "--host", host, "--cwd", repo,
                                              "show"], env=self.env, text=True, timeout=10)
            data = json.loads(answer)
            if Path(data["home"]) != home:
                raise AssertionError(f"{host} resolved {name} to the wrong project")
            result[host] = data["settings"]["judge"]["mode"]
        return result


@contextmanager
def running(binary, home, appearance):
    home = Path(home).resolve(strict=True)
    env = dict(os.environ, HOME=str(home), CFFIXED_USER_HOME=str(home))
    arguments = [binary, "-AppleInterfaceStyle", appearance, "-AppleInterfaceStyleSwitchesAutomatically", "NO",
                 "-NSRequiresAquaSystemAppearance", "YES" if appearance == "Light" else "NO",
                 "-AppleKeyboardUIMode", "3"]
    proc = subprocess.Popen(arguments, env=env)
    try:
        yield str(proc.pid)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--claude-reader", type=Path)
    parser.add_argument("--codex-reader", type=Path)
    args = parser.parse_args()
    bundle = args.bundle.resolve(strict=True)
    with (bundle / "Contents/Info.plist").open("rb") as handle:
        binary = bundle / "Contents/MacOS" / plistlib.load(handle)["CFBundleExecutable"]
    readers = installed_readers(args.claude_reader, args.codex_reader)
    out = args.evidence.resolve()
    out.mkdir(parents=True, exist_ok=False)
    base = RegistryTests()
    base.setUp()
    try:
        f = Fixture(base, readers)
        driver = f.home / "panel-driver"
        subprocess.run(["xcrun", "swiftc", "-module-cache-path", os.environ.get("CLANG_MODULE_CACHE_PATH", str(f.home / "modules")),
                        HERE / "PanelDriver.swift", "-o", driver], check=True)
        receipts = []
        appearances = {}
        def ax(command, *arguments, timeout=12):
            return subprocess.check_output([driver, pid, command, *arguments], text=True, timeout=timeout)
        def capture(theme):
            (out / f"{theme}-panel.txt").write_text(ax("dump"))
            window = ax("window").strip()
            subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-l", window,
                            str(out / f"{theme}-panel.png")], check=True, timeout=10)
            appearances[theme] = float(ax("appearance", theme, out / f"{theme}-panel.png"))

        def select(name, *, with_keyboard=False):
            ax("press", "rb.project")
            if with_keyboard:
                ax("wait", name)
                ax("menu-key", "37")  # Type L for local-notes.
                ax("menu-key", "36")  # Return commits the menu selection.
            else:
                ax("option", name)
            ax("selected", "rb.mode." + json.loads(f.settings(name).read_bytes())["judge"]["mode"])
        def mode(name, target, *, keyboard=False):
            other = next(p for p in f.paths if p != name)
            before = f.settings(other).read_bytes()
            other_mode = json.loads(before)["judge"]["mode"]
            expected = json.loads(f.settings(name).read_bytes())
            expected["judge"]["mode"] = target
            if keyboard:
                ax("focus", "rb.mode." + target)
                ax("key", "49")  # Space activates the focused mode button.
            else:
                ax("press", "rb.mode." + target)
            deadline = time.monotonic() + 5
            while json.loads(f.settings(name).read_bytes()) != expected:
                if time.monotonic() > deadline:
                    raise AssertionError(f"{name} did not persist exactly the {target} mode: {f.settings(name).read_text()}\n{ax('dump')}")
                time.sleep(0.05)
            ax("selected", "rb.mode." + target)
            readings = {p: f.observed(p) for p in f.paths}
            for project, expect in [(name, target), (other, other_mode)]:
                if set(readings[project].values()) != {expect}:
                    raise AssertionError(f"{project} host results differ: {readings}")
            if f.settings(other).read_bytes() != before:
                raise AssertionError(f"The unrelated project {other} was rewritten")
            receipts.append({"project": name, "selected": target, "readings": readings,
                             "other_settings_sha256_before_and_after": hashlib.sha256(before).hexdigest()})
        with running(binary, f.home, "Light") as pid:
            ax("press", "rb.status")
            select("sample-project")
            mode("sample-project", "each")
            capture("light")
            select("local-notes", with_keyboard=True)
            mode("local-notes", "gate")
            select("sample-project")
            mode("sample-project", "off", keyboard=True)
            ax("key", "53")  # Escape dismisses the popover.
            ax("absent", "rb.project")
            time.sleep(0.4)  # AppKit finishes the dismiss animation after removing the AX tree.
            ax("press", "rb.status")
            ax("selected", "rb.mode.off")
            # Keep the loaded project view, but make the next locked write fail.
            lock = f.paths["sample-project"][1] / ".settings.lock"
            lock.unlink(missing_ok=True)
            lock.mkdir()
            before = f.settings("sample-project").read_bytes()
            ax("press", "rb.mode.gate")
            ax("wait", "Hakem ayarı kaydedilemedi. Yeniden deneyin.")
            ax("selected", "rb.mode.off")
            if before != f.settings("sample-project").read_bytes():
                raise AssertionError("Failed save changed settings")
            lock.rmdir()
        with running(binary, f.home, "Dark") as pid:
            ax("press", "rb.status")
            ax("selected", "rb.mode.off")
            capture("dark")
        with tempfile.TemporaryDirectory(prefix="readback-empty-menu-") as empty:
            with running(binary, empty, "Light") as pid:
                ax("press", "rb.status")
                ax("wait", "Readback projesi bulunamadı.")
        report = {"app": str(bundle), "readers": {k:str(v) for k,v in readers.items()},
                  "changes": receipts, "appearance_sample_srgb_brightness": appearances, "empty_state": True, "failed_save_preserved_settings": True}
        (out / "observations.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        base.doCleanups()


if __name__ == "__main__":
    main()
