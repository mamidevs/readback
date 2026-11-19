"""Exercise the menu bar bridge as a separate process in disposable logbooks."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_project_registry as fixtures


ROOT = Path(__file__).resolve().parents[1]


class MenuSettingsTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RegistryTests(methodName="runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        self.book = fixture.logbook
        self.legacy = self.book / "legacy"
        self.legacy.mkdir()
        (self.legacy / "CURRENT.md").write_text("CURRENT   legacy\n")
        self.bridge = fixture.base / "resources" / "menu_settings.py"
        self.bridge.parent.mkdir()
        shutil.copyfile(ROOT / "app/menu-bar/menu_settings.py", self.bridge)
        for name in ("project_registry.py", "readback_settings.py", "outside_judge.py"):
            shutil.copyfile(ROOT / "scripts" / name, self.bridge.parent / name)
        self.env = {**os.environ, "HOME": str(fixture.base), "READBACK_TEAMMATES": str(self.book / "TEAMMATES.md")}

    def command(self, *argv, error=False):
        run = subprocess.run([sys.executable, "-E", "-s", "-B", str(self.bridge), *argv],
                             env=self.env, capture_output=True, text=True, timeout=15)
        if error:
            self.assertNotEqual(run.returncode, 0)
            return json.loads(run.stderr)["error"]
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def projects(self):
        return self.command("projects")["projects"]

    def test_get_creates_no_files_and_set_changes_only_selected_mode(self):
        self.fixture.activate()
        active = self.fixture.home
        active_settings = active / "settings.json"
        initial = {"schema_version": 1, "judge": {"max_fail_rounds": 2}, "commit_check": "off"}
        fixtures.write_json(active_settings, initial)
        before = fixtures.tree_state(self.book)
        items = {p["name"]: p["id"] for p in self.projects()}
        self.assertEqual(set(items), {"legacy", "sample-project"})
        for key in items.values():
            self.assertEqual(self.command("get", key)["mode"], "gate")
        self.assertEqual(fixtures.tree_state(self.book), before)
        active_bytes = active_settings.read_bytes()
        for mode in ("each", "off", "gate"):
            self.assertEqual(self.command("set", items["legacy"], mode)["mode"], mode)
            self.assertEqual(active_settings.read_bytes(), active_bytes)
        legacy_bytes = (self.legacy / "settings.json").read_bytes()
        self.command("set", items["sample-project"], "each")
        initial["judge"]["mode"] = "each"
        self.assertEqual(json.loads(active_settings.read_bytes()), initial)
        self.assertEqual((self.legacy / "settings.json").read_bytes(), legacy_bytes)

    def test_routes_for_each_host_and_tier_preserve_other_settings_and_project(self):
        self.fixture.activate()
        ids = {project["name"]: project["id"] for project in self.projects()}
        selected = ids["sample-project"]
        dest = self.fixture.home / "settings.json"
        fixtures.write_json(dest, {"schema_version": 1, "judge": {"max_fail_rounds": 2}})
        other = self.legacy / "settings.json"
        fixtures.write_json(other, {"schema_version": 1, "commit_check": "off"})
        original_other = other.read_bytes()
        expected = self.command("get", selected)["routes"]
        for host, tier, model, effort in [
            ("claude", "step", "gpt-6.2-sol", "low"),
            ("claude", "gate", "gpt-6.2-astra", "xhigh"),
            ("codex", "step", "claude-sonnet-4-6", "medium"),
            ("codex", "gate", "claude-fable-5-2", "max"),
        ]:
            provider = {"claude": "codex", "codex": "claude"}[host]
            answer = self.command("set-route", selected, host, tier, model, effort)
            pair = {"model": model, "effort": effort}
            expected[host][tier] = pair
            self.assertEqual(answer["routes"], expected)
            self.assertEqual(self.command("get", selected), answer)
            self.assertIn(effort, answer["efforts"][host])
            saved = json.loads(dest.read_bytes())["judge"]
            self.assertEqual(saved["routes"][provider][tier], pair)
            self.assertEqual(saved["max_fail_rounds"], 2)
            self.assertEqual(other.read_bytes(), original_other)

    def test_first_route_write_preserves_effective_legacy_routes(self):
        key = self.projects()[0]["id"]
        table = self.book / "TEAMMATES.md"
        table.write_text("judge | claude | codex | gpt-6.2-sol | medium\n"
                         "judge | codex | claude | claude-fable-5-2 | low\n")
        expected = self.command("get", key)["routes"]
        self.assertEqual(expected["claude"]["step"], {"model": "gpt-6.2-sol", "effort": "medium"})
        self.assertEqual(expected["codex"]["gate"], {"model": "claude-fable-5-2", "effort": "low"})
        self.assertFalse((self.legacy / "settings.json").exists())
        expected["claude"]["gate"] = {"model": "gpt-6.2-astra", "effort": "high"}
        self.assertEqual(self.command("set-route", key, "claude", "gate", "gpt-6.2-astra", "high")["routes"], expected)
        table.unlink()
        self.assertEqual(self.command("get", key)["routes"], expected)

    def test_first_mode_write_preserves_effective_legacy_routes(self):
        key = self.projects()[0]["id"]
        (self.book / "TEAMMATES.md").write_text("judge | claude | codex | gpt-6.2-sol | low\n")
        original = self.command("get", key)["routes"]
        self.assertEqual(self.command("set", key, "each")["routes"], original)
        self.assertEqual(original["claude"]["gate"]["model"], "gpt-6.2-sol")

    def test_invalid_pair_does_not_persist_either_field(self):
        key = self.projects()[0]["id"]
        path = self.legacy / "settings.json"
        fixtures.write_json(path, {"schema_version": 1, "judge": {"mode": "each"}})
        original = path.read_bytes()
        for model, effort in [("invalid model", "max"), ("fable", "ultra"), ("fable", "bogus")]:
            self.assertEqual(self.command("set-route", key, "codex", "step", model, effort, error=True),
                             "INVALID_SETTINGS")
            self.assertEqual(path.read_bytes(), original)

    def test_reused_name_requires_new_selection(self):
        prior = self.projects()[0]["id"]
        self.legacy.rename(self.book / "renamed")
        self.legacy.mkdir()
        (self.legacy / "CURRENT.md").touch()
        self.assertEqual(self.command("set", prior, "each", error=True), "NOT_FOUND")
        self.assertFalse((self.legacy / "settings.json").exists())

    def test_catalog_excludes_staged_unregistered_marked_and_aliased_homes(self):
        (self.fixture.home / "CURRENT.md").touch()
        (self.book / "alias").symlink_to(self.legacy, target_is_directory=True)
        other = self.book / "copy"
        other.mkdir()
        (other / "CURRENT.md").touch()
        shutil.copyfile(self.fixture.home / "project.json", other / "project.json")
        self.assertEqual([p["location"] for p in self.projects()], [str(self.legacy)])
        self.fixture.registry.write_text("invalid")
        self.assertEqual(self.command("projects", error=True), "INVALID")

    def test_failed_update_keeps_original_settings(self):
        key = self.projects()[0]["id"]
        dest = self.legacy / "settings.json"
        dest.write_text('{"schema_version":2}')
        self.assertEqual(self.command("set", key, "off", error=True), "INVALID_SETTINGS")
        self.assertEqual(dest.read_text(), '{"schema_version":2}')
        dest.unlink()
        target = self.fixture.base / "unrelated"
        target.write_text('{"schema_version":1}')
        dest.symlink_to(target)
        self.assertEqual(self.command("set", key, "each", error=True), "INVALID_SETTINGS")
        self.assertEqual(target.read_text(), '{"schema_version":1}')
        self.assertTrue(dest.is_symlink())

    def test_empty_and_unregistered_logbook(self):
        with tempfile.TemporaryDirectory() as fresh:
            self.env["HOME"] = str(Path(fresh).resolve())
            self.assertEqual(self.projects(), [])
            book = Path(fresh) / "logbook"
            self.assertFalse(book.exists())
            home = book / "readback"
            home.mkdir(parents=True)
            (home / "CURRENT.md").touch()
            key = self.projects()[0]["id"]
            self.assertEqual(self.command("set", key, "each")["mode"], "each")
            self.assertFalse((book / ".readback").exists())


if __name__ == "__main__":
    unittest.main()
