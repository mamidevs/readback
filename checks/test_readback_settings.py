#!/usr/bin/env python3
"""Checks for scripts/readback_settings.py and schemas/settings.schema.json.

Run: python3 -B checks/test_readback_settings.py <Class ...>
Every test builds its own registry, logbook, judge table and home folder in a temporary
folder. No provider is contacted and no file of the owner is read or written.
Digests come from canonical JSON strings that are written by hand below.
"""

import contextlib
import copy
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SCRIPT = SCRIPTS / "readback_settings.py"
SCHEMA = ROOT / "schemas" / "settings.schema.json"
sys.path.insert(0, str(SCRIPTS))
import outside_judge  # noqa: E402,F401  (it sets dont_write_bytecode before the other imports)
import project_registry as pr  # noqa: E402
import readback_settings as rs  # noqa: E402

# ---- canonical JSON written by hand: sorted keys, "," and ":", no spaces, no line feed ----
DEF_CANON = (
    '{"commit_check":"observe","judge":{"deadline_seconds":{"gate":900,"step":300},'
    '"max_fail_rounds":3,"mode":"gate","provider":"opposite","routes":{'
    '"claude":{"gate":{"effort":"high","model":"fable"},"step":{"effort":"high","model":"fable"}},'
    '"codex":{"gate":{"effort":"high","model":"gpt-6-astra"},"step":{"effort":"high","model":"gpt-6-astra"}}}},'
    '"schema_version":1}'
)
DEF_V1_CANON = DEF_CANON  # no file and the file {"schema_version": 1} have the same effective settings
MODE_EACH_CANON = (
    '{"commit_check":"observe","judge":{"deadline_seconds":{"gate":900,"step":300},'
    '"max_fail_rounds":3,"mode":"each","provider":"opposite","routes":{'
    '"claude":{"gate":{"effort":"high","model":"fable"},"step":{"effort":"high","model":"fable"}},'
    '"codex":{"gate":{"effort":"high","model":"gpt-6-astra"},"step":{"effort":"high","model":"gpt-6-astra"}}}},'
    '"schema_version":1}'
)
ALL_CANON = (
    '{"commit_check":"off","judge":{"deadline_seconds":{"gate":3600,"step":30},'
    '"max_fail_rounds":10,"mode":"each","provider":"opposite","routes":{'
    '"claude":{"gate":{"effort":"max_1-a","model":"fable-2:x/y"},"step":{"effort":"xhigh","model":"claude-fable-5.1"}},'
    '"codex":{"gate":{"effort":"medium","model":"o3.mini"},"step":{"effort":"low","model":"gpt-7"}}}},'
    '"schema_version":1}'
)
CLAUDE_STEP_BOTH_CANON = (
    '{"commit_check":"observe","judge":{"deadline_seconds":{"gate":900,"step":300},'
    '"max_fail_rounds":3,"mode":"gate","provider":"opposite","routes":{'
    '"claude":{"gate":{"effort":"high","model":"fable"},"step":{"effort":"max","model":"claude-fable-5"}},'
    '"codex":{"gate":{"effort":"high","model":"gpt-6-astra"},"step":{"effort":"high","model":"gpt-6-astra"}}}},'
    '"schema_version":1}'
)
CODEX_GATE_MODEL_CANON = (
    '{"commit_check":"observe","judge":{"deadline_seconds":{"gate":900,"step":300},'
    '"max_fail_rounds":3,"mode":"gate","provider":"opposite","routes":{'
    '"claude":{"gate":{"effort":"high","model":"fable"},"step":{"effort":"high","model":"fable"}},'
    '"codex":{"gate":{"effort":"high","model":"gpt-7"},"step":{"effort":"high","model":"gpt-6-astra"}}}},'
    '"schema_version":1}'
)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def defaults():
    """The effective settings of D3, written out here without the module."""
    return {
        "schema_version": 1,
        "judge": {
            "mode": "gate", "provider": "opposite",
            "routes": {
                "claude": {"step": {"model": "fable", "effort": "high"}, "gate": {"model": "fable", "effort": "high"}},
                "codex": {"step": {"model": "gpt-6-astra", "effort": "high"},
                          "gate": {"model": "gpt-6-astra", "effort": "high"}},
            },
            "deadline_seconds": {"step": 300, "gate": 900},
            "max_fail_rounds": 3,
        },
        "commit_check": "observe",
    }


def with_version(settings):
    return {"schema_version": 1, **settings}


def dump_file(obj):
    """The bytes the module writes: two-space indent, order kept, one line feed at the end."""
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def jbytes(obj):
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


ALL_FILE = {
    "schema_version": 1,
    "judge": {
        "mode": "each", "provider": "opposite",
        "routes": {
            "claude": {"step": {"model": "claude-fable-5.1", "effort": "xhigh"},
                       "gate": {"model": "fable-2:x/y", "effort": "max_1-a"}},
            "codex": {"step": {"model": "gpt-7", "effort": "low"},
                      "gate": {"model": "o3.mini", "effort": "medium"}},
        },
        "deadline_seconds": {"step": 30, "gate": 3600},
        "max_fail_rounds": 10,
    },
    "commit_check": "off",
}
LEAF_KEYS = [
    "judge.mode", "judge.provider",
    "judge.routes.claude.step.model", "judge.routes.claude.step.effort",
    "judge.routes.claude.gate.model", "judge.routes.claude.gate.effort",
    "judge.routes.codex.step.model", "judge.routes.codex.step.effort",
    "judge.routes.codex.gate.model", "judge.routes.codex.gate.effort",
    "judge.deadline_seconds.step", "judge.deadline_seconds.gate",
    "judge.max_fail_rounds", "commit_check",
]


def set_path(obj, dotted, value):
    node = obj
    *parents, leaf = dotted.split(".")
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value
    return obj


def file_with(**leaves):
    """A file object with schema_version 1 and the given dotted leaves, keyed with `__` for `.`."""
    obj = {"schema_version": 1}
    for key, value in leaves.items():
        set_path(obj, key.replace("__", "."), value)
    return obj


# ---- the files F1 to F17 of D9 ----
# (label, file bytes or None for no file, expected): expected is ("ok", effective settings, canonical text)
# or ("error", dotted key path or None, message tail). The message is "<path>: <dotted>: <tail>".
OBJECT_PLACES = [
    "judge", "judge.routes", "judge.routes.claude", "judge.routes.codex",
    "judge.routes.claude.step", "judge.routes.claude.gate",
    "judge.routes.codex.step", "judge.routes.codex.gate", "judge.deadline_seconds",
]


def fixture_cases():
    cases = []

    def ok(label, obj, settings, canon):
        cases.append((label, dump_file(obj) if obj is not None else None, ("ok", settings, canon), obj))

    def bad(label, raw, dotted, tail, obj=None):
        cases.append((label, raw, ("error", dotted, tail), obj))

    def bad_obj(label, obj, dotted, tail):
        bad(label, dump_file(obj), dotted, tail, obj)

    ok("F1", None, defaults(), DEF_CANON)
    ok("F2", {"schema_version": 1}, with_version(defaults()), DEF_V1_CANON)
    ok("F3", ALL_FILE, ALL_FILE, ALL_CANON)
    each = with_version(defaults())
    each["judge"]["mode"] = "each"
    ok("F4", file_with(judge__mode="each"), each, MODE_EACH_CANON)
    both = with_version(defaults())
    both["judge"]["routes"]["claude"]["step"] = {"model": "claude-fable-5", "effort": "max"}
    ok("F5 both leaves", file_with(judge__routes__claude__step={"model": "claude-fable-5", "effort": "max"}),
       both, CLAUDE_STEP_BOTH_CANON)
    model_only = with_version(defaults())
    model_only["judge"]["routes"]["codex"]["gate"]["model"] = "gpt-7"
    ok("F5 model only", file_with(judge__routes__codex__gate={"model": "gpt-7"}), model_only, CODEX_GATE_MODEL_CANON)
    bad_obj("F6", {"schema_version": 1, "other": 1}, "other", "unknown key")
    bad_obj("F7", file_with(judge__routes__claude__step={"model": "x", "extra": 1}),
            "judge.routes.claude.step.extra", "unknown key")
    bad_obj("F8", file_with(judge__mode="auto"), "judge.mode", "invalid value")
    bad_obj("F9", file_with(judge__provider="same"), "judge.provider", "invalid value")
    bad_obj("F10", file_with(commit_check="enforce"), "commit_check", "invalid value")
    for tier in ("step", "gate"):
        for value in (29, 3601, True, "300"):
            bad_obj("F11 %s %r" % (tier, value), file_with(**{"judge__deadline_seconds__" + tier: value}),
                    "judge.deadline_seconds." + tier, "invalid value")
    for value in (0, 11):
        bad_obj("F12 %r" % value, file_with(judge__max_fail_rounds=value), "judge.max_fail_rounds", "invalid value")
    bad_obj("F13 model with a space", file_with(judge__routes__claude__step__model="a b"),
            "judge.routes.claude.step.model", "invalid value")
    bad_obj("F13 empty effort", file_with(judge__routes__codex__gate__effort=""),
            "judge.routes.codex.gate.effort", "invalid value")
    bad("F14 missing", dump_file({"judge": {"mode": "each"}}), "schema_version", "is required")
    bad("F14 two", dump_file({"schema_version": 2}), "schema_version", "must be 1")
    bad("F14 true", b'{"schema_version": true}\n', "schema_version", "must be 1")
    bad("F15 not JSON", b"settings are not JSON\n", None, None)
    bad("F15 list", b'[{"schema_version": 1}]\n', "$", "must be an object")
    bad_obj("F16 model fabé", file_with(judge__routes__claude__step__model="fabé"),
            "judge.routes.claude.step.model", "invalid value")
    bad("F16 byte order mark", b"\xef\xbb\xbf" + dump_file({"schema_version": 1}), None, None)
    for place in OBJECT_PLACES:
        for value in (None, 5, "x", [], [1]):
            bad_obj("F17 %s=%r" % (place, value), set_path({"schema_version": 1}, place, value), place,
                    "must be an object")
    return cases


CASES = fixture_cases()

# Single-leaf instances that test every boundary: (dotted key, value, valid)
BOUNDARIES = []
for _tier in ("step", "gate"):
    for _value, _valid in ((30, True), (3600, True), (29, False), (3601, False), (0, False), (-1, False),
                           (True, False), (False, False), ("300", False), (29.0, False), (3601.0, False), (1.5, False),
                           (None, False), ([], False)):
        BOUNDARIES.append(("judge.deadline_seconds." + _tier, _value, _valid))
for _value, _valid in ((1, True), (10, True), (0, False), (11, False), (True, False), (0.0, False), (10.5, False), ("3", False)):
    BOUNDARIES.append(("judge.max_fail_rounds", _value, _valid))
for _value, _valid in (("off", True), ("gate", True), ("each", True), ("auto", False), ("", False), ("Gate", False),
                       (None, False), (1, False), ([], False)):
    BOUNDARIES.append(("judge.mode", _value, _valid))
for _value, _valid in (("opposite", True), ("same", False), ("", False), (None, False), (["opposite"], False)):
    BOUNDARIES.append(("judge.provider", _value, _valid))
for _value, _valid in (("off", True), ("observe", True), ("enforce", False), ("", False), (None, False), (0, False)):
    BOUNDARIES.append(("commit_check", _value, _valid))
for _host in ("claude", "codex"):
    for _tier in ("step", "gate"):
        _base = "judge.routes.%s.%s." % (_host, _tier)
        for _value, _valid in (("a", True), ("7b", True), ("A.b_c-d:e/f", True), ("claude-fable-5.1", True),
                               ("x" * 80, True), ("-x", False), ("_x", False), (".x", False), ("a b", False),
                               ("", False), ("x\n", False), ("fabé", False), ("١", False), (None, False),
                               (5, False), (True, False), (["a"], False)):
            BOUNDARIES.append((_base + "model", _value, _valid))
        for _value, _valid in (("high", True), ("a_b-1", True), ("0", True), ("-", True), ("_", True), ("a:b", False),
                               ("a.b", False), ("a/b", False), ("a b", False), ("", False), ("x\n", False),
                               ("é", False), ("١", False), (None, False), (5, False), (["a"], False)):
            BOUNDARIES.append((_base + "effort", _value, _valid))


def snapshot(root):
    """Names, sizes and digests of every file below root, without Git's own folders."""
    found = {}
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            path = Path(current) / name
            if path.is_symlink():
                found[str(path)] = ("link", os.readlink(path))
            else:
                found[str(path)] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        for name in dirs:
            found[str(Path(current) / name)] = ("dir",)
    return found


def temp_home(test):
    folder = tempfile.TemporaryDirectory(prefix="readback-settings-home-")
    test.addCleanup(folder.cleanup)
    home = Path(folder.name).resolve()
    return home


def entries(home):
    return sorted(p.name for p in Path(home).iterdir())


class Guarded(unittest.TestCase):
    """HOME is an empty temporary folder for each test and for each process it starts. It must stay empty."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="readback-settings-safehome-")
        self.addCleanup(folder.cleanup)
        self.safe_home = Path(folder.name).resolve()
        patch = mock.patch.dict(os.environ, {"HOME": str(self.safe_home)})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.assert_safe_home_empty)

    def assert_safe_home_empty(self):
        self.assertEqual(sorted(p.name for p in self.safe_home.iterdir()), [])


# =====================================================================
class SettingsTests(Guarded):
    """ACC-1: the loader on the files F1 to F17 and the boundaries of D2."""

    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        self.path = str(self.home / "settings.json")

    def put(self, raw):
        if raw is not None:
            (self.home / "settings.json").write_bytes(raw)

    def test_files_f1_to_f17(self):
        for label, raw, expected, _obj in CASES:
            with self.subTest(label):
                for name in entries(self.home):
                    (self.home / name).unlink()
                self.put(raw)
                if expected[0] == "ok":
                    _kind, settings, canon = expected
                    self.assertEqual(rs.load(self.home), {
                        "settings": settings, "source": "defaults" if raw is None else "file",
                        "path": self.path, "digest": sha(canon)})
                    self.assertEqual(rs.load(str(self.home)), rs.load(self.home))
                else:
                    _kind, dotted, tail = expected
                    with self.assertRaises(rs.SettingsError) as caught:
                        rs.load(self.home)
                    self.assertEqual(caught.exception.code, "INVALID_SETTINGS")
                    if dotted is None:
                        self.assertTrue(str(caught.exception).startswith(self.path + ": not valid JSON: "),
                                        str(caught.exception))
                    else:
                        self.assertEqual(str(caught.exception), "%s: %s: %s" % (self.path, dotted, tail))

    def test_no_file_and_an_empty_settings_file_have_equal_settings_and_digest(self):
        hand = ('{"commit_check":"observe","judge":{"deadline_seconds":{"gate":900,"step":300},"max_fail_rounds":3,'
                '"mode":"gate","provider":"opposite","routes":{"claude":{"gate":{"effort":"high","model":"fable"},'
                '"step":{"effort":"high","model":"fable"}},"codex":{"gate":{"effort":"high","model":"gpt-6-astra"},'
                '"step":{"effort":"high","model":"gpt-6-astra"}}}},"schema_version":1}')
        none = rs.load(self.home)
        (self.home / "settings.json").write_bytes(b'{"schema_version": 1}')
        empty = rs.load(self.home)
        self.assertEqual(none["settings"], empty["settings"])
        self.assertEqual(none["settings"]["schema_version"], 1)
        self.assertEqual(none["digest"], hashlib.sha256(hand.encode()).hexdigest())
        self.assertEqual(empty["digest"], hashlib.sha256(hand.encode()).hexdigest())
        self.assertEqual((none["source"], empty["source"]), ("defaults", "file"))
        self.assertEqual(rs.DEFAULTS["schema_version"], 1)

    def test_error_messages_of_unparsable_files(self):
        self.put(b"\xef\xbb\xbf" + dump_file({"schema_version": 1}))
        with self.assertRaises(rs.SettingsError) as caught:
            rs.load(self.home)
        self.assertEqual(caught.exception.code, "INVALID_SETTINGS")
        self.assertTrue(str(caught.exception).startswith(self.path + ": not valid JSON: Unexpected UTF-8 BOM"))
        for raw in (b"", b"{", b'{"schema_version": 1,}', b"\xff\xfe\x00", b'{"schema_version": NaN}',
                    b'{"schema_version": 1, "schema_version": 1}', b'{"schema_version": Infinity}',
                    b'{"schema_version": 1, "judge": {"mode": "each", "mode": "each"}}', b"[" * 100000):
            with self.subTest(raw[:30]):
                self.put(raw)
                with self.assertRaises(rs.SettingsError) as caught:
                    rs.load(self.home)
                self.assertEqual(caught.exception.code, "INVALID_SETTINGS")
                self.assertTrue(str(caught.exception).startswith(self.path + ": not valid JSON: "))

    def test_scalars_and_null_at_the_top(self):
        for raw in (b"5", b"null", b'"x"', b"true", b"[]", b"{}"):
            with self.subTest(raw):
                self.put(raw)
                with self.assertRaises(rs.SettingsError) as caught:
                    rs.load(self.home)
                self.assertEqual(caught.exception.code, "INVALID_SETTINGS")
                tail = "is required" if raw == b"{}" else "must be an object"
                dotted = "schema_version" if raw == b"{}" else "$"
                self.assertEqual(str(caught.exception), "%s: %s: %s" % (self.path, dotted, tail))

    def test_schema_version_values(self):
        for value in (1.0, "1", None, 0, -1, [1], {"v": 1}, 2, True, False):
            with self.subTest(value):
                self.put(json.dumps({"schema_version": value}).encode())
                with self.assertRaises(rs.SettingsError) as caught:
                    rs.load(self.home)
                self.assertEqual(str(caught.exception), "%s: schema_version: must be 1" % self.path)

    def test_unreadable_path_is_an_invalid_settings_error(self):
        (self.home / "settings.json").mkdir()
        with self.assertRaises(rs.SettingsError) as caught:
            rs.load(self.home)
        self.assertEqual(caught.exception.code, "INVALID_SETTINGS")
        self.assertTrue(str(caught.exception).startswith(self.path + ": cannot read the file: "))
        (self.home / "settings.json").rmdir()
        (self.home / "settings.json").symlink_to(self.home / "missing-target")
        self.assertEqual(rs.load(self.home)["source"], "defaults")  # a dangling link counts as no file

    def test_boundaries_of_every_leaf(self):
        for key, value, valid in BOUNDARIES:
            with self.subTest(key=key, value=value):
                self.put(dump_file(set_path({"schema_version": 1}, key, value)))
                if valid:
                    expected = with_version(defaults())
                    set_path(expected, key, value)
                    loaded = rs.load(self.home)
                    self.assertEqual(loaded["settings"], expected)
                    self.assertEqual(loaded["source"], "file")
                else:
                    with self.assertRaises(rs.SettingsError) as caught:
                        rs.load(self.home)
                    self.assertEqual(caught.exception.code, "INVALID_SETTINGS")
                    self.assertEqual(str(caught.exception), "%s: %s: invalid value" % (self.path, key))

    def test_defaults_fill_each_missing_leaf_on_its_own(self):
        for key in LEAF_KEYS:
            with self.subTest(key):
                value = ALL_FILE
                for part in key.split("."):
                    value = value[part]
                self.put(dump_file(set_path({"schema_version": 1}, key, value)))
                expected = with_version(defaults())
                set_path(expected, key, value)
                self.assertEqual(rs.load(self.home)["settings"], expected)

    def test_a_configured_claude_route_has_no_fable_restriction(self):
        for model in ("gpt-6-astra", "opus", "claude-sonnet-5"):
            self.put(dump_file(file_with(judge__routes__claude__step__model=model)))
            self.assertEqual(rs.load(self.home)["settings"]["judge"]["routes"]["claude"]["step"]["model"], model)

    def test_no_file_in_a_missing_home(self):
        missing = self.home / "not-there"
        self.assertEqual(rs.load(missing), {
            "settings": defaults(), "source": "defaults", "path": str(missing / "settings.json"),
            "digest": sha(DEF_CANON)})

    def test_load_does_not_change_the_defaults_object(self):
        before = copy.deepcopy(rs.DEFAULTS)
        loaded = rs.load(self.home)
        loaded["settings"]["judge"]["routes"]["claude"]["step"]["model"] = "changed"
        loaded["settings"]["judge"]["mode"] = "off"
        self.assertEqual(rs.DEFAULTS, before)
        self.assertEqual(rs.load(self.home)["settings"], defaults())

    def test_digest_rules(self):
        self.assertEqual(rs.canonical({"k": "é"}), b'{"k":"\xc3\xa9"}')
        self.assertEqual(rs.canonical({"b": 1, "a": [1, {"d": 0, "c": 2}], "e": {}, "f": []}),
                         b'{"a":[1,{"c":2,"d":0}],"b":1,"e":{},"f":[]}')
        self.assertEqual(rs.canonical({}), b"{}")
        self.assertEqual(rs.digest({"k": "é"}), hashlib.sha256(b'{"k":"\xc3\xa9"}').hexdigest())
        self.assertEqual(rs.digest({"b": 1, "a": 2}), hashlib.sha256(b'{"a":2,"b":1}').hexdigest())
        self.assertEqual(rs.canonical(rs.DEFAULTS), DEF_CANON.encode("utf-8"))
        self.assertFalse(rs.canonical(rs.DEFAULTS).endswith(b"\n"))
        self.assertFalse(rs.canonical(rs.DEFAULTS).startswith(b"\xef\xbb\xbf"))

    def test_the_digest_follows_the_effective_settings_not_the_file_text(self):
        self.put(b'{ "schema_version" : 1 ,\n "judge": {"mode": "each"} }')
        self.assertEqual(rs.load(self.home)["digest"], sha(MODE_EACH_CANON))
        self.put(b'{"judge":{"mode":"each"},"schema_version":1}')
        self.assertEqual(rs.load(self.home)["digest"], sha(MODE_EACH_CANON))
        self.put(dump_file(file_with(judge__mode="each", commit_check="observe")))
        self.assertEqual(rs.load(self.home)["digest"], sha(MODE_EACH_CANON))
        digests = {sha(c) for c in (DEF_CANON, MODE_EACH_CANON, ALL_CANON)}
        self.assertEqual(len(digests), 3)

    def test_leaves_derive_from_the_defaults(self):
        found = rs.leaves()
        self.assertEqual(sorted(found), sorted(LEAF_KEYS))
        self.assertEqual(found["judge.deadline_seconds.step"], 300)
        self.assertEqual(found["judge.max_fail_rounds"], 3)
        self.assertEqual(found["judge.routes.codex.gate.model"], "gpt-6-astra")
        self.assertEqual(rs.DEFAULTS, defaults())

    def test_check_names_the_file_in_its_message(self):
        with self.assertRaises(rs.SettingsError) as caught:
            rs.check({"schema_version": 1, "x": 1}, "/some/where/settings.json")
        self.assertEqual(str(caught.exception), "/some/where/settings.json: x: unknown key")
        self.assertEqual(caught.exception.code, "INVALID_SETTINGS")
        self.assertIsNone(rs.check(copy.deepcopy(ALL_FILE), "p"))

    def test_error_object(self):
        error = rs.SettingsError("CODE_X", "message y")
        self.assertEqual((error.code, str(error)), ("CODE_X", "message y"))
        self.assertIsInstance(error, Exception)


# =====================================================================
CHILD = r"""
import json, os, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import readback_settings as rs
mode, home, ready, go = sys.argv[2:6]
real = os.fsync
def slow(fd):
    time.sleep(0.02)
    real(fd)
os.fsync = slow
open(ready, "w").close()
while not os.path.exists(go):
    time.sleep(0.001)
if mode == "update":
    rs.update(home, sys.argv[6], json.loads(sys.argv[7]))
else:
    rs.migrate_teammates(home, sys.argv[6], True)
"""

TABLE_BOTH = (
    "| role | host | provider | model | effort |\n"
    "| judge | claude | codex | gpt-9 | xhigh |\n"
    "| judge | codex | claude | claude-fable-7 | max |\n"
)


class UpdateTests(Guarded):
    """ACC-2: update, the file mode, rejected values, injected failures and concurrent writers."""

    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        self.file = self.home / "settings.json"

    def listing(self):
        return entries(self.home)

    # -- exactly the named key -------------------------------------------------
    def test_update_from_no_file_writes_exactly_the_named_key(self):
        result = rs.update(self.home, "judge.mode", "each")
        self.assertEqual(self.file.read_bytes(),
                         b'{\n  "schema_version": 1,\n  "judge": {\n    "mode": "each"\n  }\n}\n')
        self.assertEqual(result, {"settings": with_version({**defaults(), "judge": {
            **defaults()["judge"], "mode": "each"}}), "source": "file", "path": str(self.file),
            "digest": sha(MODE_EACH_CANON)})
        self.assertEqual(self.listing(), [".settings.lock", "settings.json"])

    def test_update_changes_one_key_and_keeps_the_others(self):
        self.file.write_bytes(dump_file(ALL_FILE))
        for key in LEAF_KEYS:
            with self.subTest(key):
                self.file.write_bytes(dump_file(ALL_FILE))
                new = {"judge.mode": "off", "judge.provider": "opposite", "judge.max_fail_rounds": 2,
                       "judge.deadline_seconds.step": 31, "judge.deadline_seconds.gate": 3599,
                       "commit_check": "observe"}.get(key, "changed-1")
                result = rs.update(self.home, key, new)
                expected = copy.deepcopy(ALL_FILE)
                set_path(expected, key, new)
                self.assertEqual(json.loads(self.file.read_bytes()), expected)
                self.assertEqual(self.file.read_bytes(), dump_file(expected))
                self.assertEqual(result["settings"], expected)
                self.assertEqual(result["source"], "file")
                self.assertEqual(result["path"], str(self.file))
                self.assertEqual(result["digest"], rs.load(self.home)["digest"])

    def test_update_adds_a_key_to_an_existing_partial_file(self):
        self.file.write_bytes(dump_file(file_with(judge__mode="each")))
        rs.update(self.home, "judge.routes.claude.step.model", "m1")
        rs.update(self.home, "judge.routes.claude.step.effort", "low")
        rs.update(self.home, "commit_check", "off")
        self.assertEqual(self.file.read_bytes(), dump_file({
            "schema_version": 1,
            "judge": {"mode": "each", "routes": {"claude": {"step": {"model": "m1", "effort": "low"}}}},
            "commit_check": "off"}))
        rs.update(self.home, "judge.mode", "off")
        self.assertEqual(json.loads(self.file.read_bytes())["judge"]["mode"], "off")

    def test_update_returns_the_object_of_load(self):
        self.assertEqual(rs.update(str(self.home), "commit_check", "off"), rs.load(self.home))

    def test_update_accepts_a_whole_object_when_the_result_is_valid(self):
        rs.update(self.home, "judge.deadline_seconds", {"step": 40})
        self.assertEqual(json.loads(self.file.read_bytes()),
                         {"schema_version": 1, "judge": {"deadline_seconds": {"step": 40}}})

    # -- rejected values -------------------------------------------------------
    def test_a_rejected_value_leaves_the_bytes_unchanged(self):
        original = dump_file(file_with(judge__mode="each"))
        cases = [("judge.mode", "auto"), ("judge.mode", None), ("judge.provider", "same"), ("commit_check", "enforce"),
                 ("judge.max_fail_rounds", 0), ("judge.max_fail_rounds", 11), ("judge.max_fail_rounds", True),
                 ("judge.deadline_seconds.step", 29), ("judge.deadline_seconds.gate", 3601),
                 ("judge.deadline_seconds.step", "300"), ("judge.routes.claude.step.model", "a b"),
                 ("judge.routes.claude.step.effort", ""), ("judge.routes.codex.gate.model", "fabé"),
                 ("judge.routes", "x"), ("judge", None), ("judge.nope", 1), ("nope", 1), ("schema_version", 2),
                 ("schema_version", True), ("judge.routes.claude.step", {"model": "x", "extra": 1})]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.file.write_bytes(original)
                with self.assertRaises(rs.SettingsError) as caught:
                    rs.update(self.home, key, value)
                self.assertEqual(caught.exception.code, "INVALID_SETTINGS")
                self.assertTrue(str(caught.exception).startswith(str(self.file) + ": " + key.split(".")[0]) or key == "",
                                str(caught.exception))
                self.assertEqual(self.file.read_bytes(), original)
                self.assertEqual(self.listing(), [".settings.lock", "settings.json"])

    def test_a_rejected_value_names_the_dotted_key(self):
        cases = [("judge.mode", "auto", "judge.mode: invalid value"),
                 ("judge.deadline_seconds.gate", 3601, "judge.deadline_seconds.gate: invalid value"),
                 ("judge.routes.codex.step.effort", "a b", "judge.routes.codex.step.effort: invalid value"),
                 ("judge.nope", 1, "judge.nope: unknown key"), ("nope", 1, "nope: unknown key"),
                 ("", 1, ": unknown key"), ("judge..mode", "each", "judge.: unknown key"),
                 ("judge.mode.x", 1, "judge.mode.x: cannot set below a value"),
                 ("judge.routes.claude.step.model.x", 1, "judge.routes.claude.step.model.x: cannot set below a value")]
        for key, value, tail in cases:
            with self.subTest(key):
                self.file.write_bytes(dump_file(file_with(judge__mode="each", judge__routes__claude__step__model="m")))
                before = self.file.read_bytes()
                with self.assertRaises(rs.SettingsError) as caught:
                    rs.update(self.home, key, value)
                self.assertEqual(str(caught.exception), "%s: %s" % (self.file, tail))
                self.assertEqual(self.file.read_bytes(), before)

    def test_a_rejected_value_creates_no_settings_file(self):
        with self.assertRaises(rs.SettingsError):
            rs.update(self.home, "judge.mode", "auto")
        self.assertFalse(self.file.exists())
        self.assertEqual([n for n in self.listing() if n != ".settings.lock"], [])

    def test_an_invalid_existing_file_is_not_repaired(self):
        for raw in (b"not json", dump_file({"schema_version": 1, "other": 1}), b'{"judge": {}}', b"\xef\xbb\xbf{}"):
            with self.subTest(raw):
                self.file.write_bytes(raw)
                with self.assertRaises(rs.SettingsError) as caught:
                    rs.update(self.home, "judge.mode", "each")
                self.assertEqual(caught.exception.code, "INVALID_SETTINGS")
                self.assertTrue(str(caught.exception).startswith(str(self.file) + ": "))
                self.assertEqual(self.file.read_bytes(), raw)

    def test_the_home_folder_must_exist(self):
        missing = self.home / "absent"
        with self.assertRaises(rs.SettingsError) as caught:
            rs.update(missing, "judge.mode", "each")
        self.assertEqual(caught.exception.code, "HOME_MISSING")
        self.assertEqual(str(caught.exception), "%s: the project home folder does not exist" % missing)
        self.assertFalse(missing.exists())
        plain = self.home / "plain-file"
        plain.write_text("x")
        with self.assertRaises(rs.SettingsError) as caught:
            rs.update(plain, "judge.mode", "each")
        self.assertEqual(caught.exception.code, "HOME_MISSING")
        with self.assertRaises(rs.SettingsError) as caught:
            rs.migrate_teammates(missing, str(self.home / "t.md"), True)
        self.assertEqual(caught.exception.code, "HOME_MISSING")

    # -- mode ---------------------------------------------------------------------
    def test_the_file_has_mode_0600_under_each_umask(self):
        for umask in (0o022, 0o077, 0o000):
            with self.subTest(umask=oct(umask)):
                home = self.home / ("umask-%o" % umask)
                home.mkdir()
                old = os.umask(umask)
                try:
                    rs.update(home, "judge.mode", "each")
                    rs.update(home, "judge.mode", "off")
                finally:
                    os.umask(old)
                self.assertEqual(stat.S_IMODE((home / "settings.json").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((home / ".settings.lock").stat().st_mode), 0o600)
                self.assertEqual(entries(home), [".settings.lock", "settings.json"])

    def test_a_rename_replaces_a_file_with_a_looser_mode(self):
        self.file.write_bytes(dump_file({"schema_version": 1}))
        os.chmod(self.file, 0o666)
        rs.update(self.home, "judge.mode", "each")
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)

    # -- injected failures ----------------------------------------------------
    def inject(self, target, call_number=None, replacement=None):
        """Patch os.<target>; fail on the given call (1-based) or on every call."""
        real = getattr(os, target)
        calls = []

        def wrapper(*args, **kwargs):
            calls.append(args)
            if call_number is None or call_number == len(calls):
                if replacement is not None:
                    return replacement(*args, **kwargs)
                raise OSError(errno.EIO, "injected " + target)
            return real(*args, **kwargs)

        return mock.patch.object(os, target, wrapper), calls

    def test_the_steps_run_in_order(self):
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            events.append("fsync " + ("file" if stat.S_ISREG(os.fstat(fd).st_mode) else
                                      "folder" if stat.S_ISDIR(os.fstat(fd).st_mode) else "other"))
            real_fsync(fd)

        def replace(source, target):
            events.append("replace")
            self.assertEqual(Path(target), self.file)
            self.assertEqual(Path(source).parent, self.home)
            self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
            real_replace(source, target)

        with mock.patch.object(os, "fsync", fsync), mock.patch.object(os, "replace", replace):
            rs.update(self.home, "judge.mode", "each")
        self.assertEqual(events, ["fsync file", "replace", "fsync folder"])
        self.assertEqual(self.listing(), [".settings.lock", "settings.json"])

    def assert_failure(self, target, call_number, earlier, new, replacement=None, message=None):
        self.file.write_bytes(earlier)
        patch, calls = self.inject(target, call_number, replacement)
        with patch:
            with self.assertRaises(rs.SettingsError) as caught:
                rs.update(self.home, "judge.mode", "each")
        self.assertEqual(caught.exception.code, "WRITE_FAILED")
        self.assertTrue(str(caught.exception).startswith(str(self.file) + ": "), str(caught.exception))
        if message:
            self.assertIn(message, str(caught.exception))
        self.assertEqual(self.file.read_bytes(), new if new is not None else earlier)
        self.assertEqual(self.listing(), [".settings.lock", "settings.json"])
        return calls

    def test_failure_of_the_write_keeps_the_earlier_bytes(self):
        earlier = dump_file(file_with(commit_check="off"))
        calls = self.assert_failure("write", None, earlier, None, message="injected write")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], dump_file(file_with(commit_check="off", judge__mode="each")))

    def test_a_short_write_is_a_failure(self):
        earlier = dump_file(file_with(commit_check="off"))
        self.assert_failure("write", None, earlier, None, replacement=lambda fd, data: len(data) - 1,
                            message="short write")
        self.assert_failure("write", None, earlier, None, replacement=lambda fd, data: 0, message="short write")

    def test_failure_of_the_first_fsync_keeps_the_earlier_bytes(self):
        earlier = dump_file(file_with(commit_check="off"))
        calls = self.assert_failure("fsync", 1, earlier, None, message="injected fsync")
        self.assertEqual(len(calls), 1)

    def test_failure_of_the_rename_keeps_the_earlier_bytes(self):
        earlier = dump_file(file_with(commit_check="off"))
        calls = self.assert_failure("replace", None, earlier, None, message="injected replace")
        self.assertEqual(len(calls), 1)

    def test_failure_of_the_folder_sync_leaves_the_new_bytes(self):
        earlier = dump_file(file_with(commit_check="off"))
        new = dump_file(file_with(commit_check="off", judge__mode="each"))
        calls = self.assert_failure("fsync", 2, earlier, new, message="injected fsync")
        self.assertEqual(len(calls), 2)

    def test_failure_of_the_temporary_file_creation(self):
        earlier = dump_file(file_with(commit_check="off"))
        self.file.write_bytes(earlier)

        def refuse(*args, **kwargs):
            raise OSError(errno.ENOSPC, "injected mkstemp")

        with mock.patch.object(tempfile, "mkstemp", refuse):
            with self.assertRaises(rs.SettingsError) as caught:
                rs.update(self.home, "judge.mode", "each")
        self.assertEqual(caught.exception.code, "WRITE_FAILED")
        self.assertIn("injected mkstemp", str(caught.exception))
        self.assertEqual(self.file.read_bytes(), earlier)
        self.assertEqual(self.listing(), [".settings.lock", "settings.json"])

    def test_failures_leave_no_file_when_there_was_none(self):
        for target, number in (("write", None), ("fsync", 1), ("replace", None)):
            with self.subTest(target):
                patch, _calls = self.inject(target, number)
                with patch:
                    with self.assertRaises(rs.SettingsError) as caught:
                        rs.update(self.home, "judge.mode", "each")
                self.assertEqual(caught.exception.code, "WRITE_FAILED")
                self.assertFalse(self.file.exists())
                self.assertEqual(self.listing(), [".settings.lock"])

    def test_a_failure_of_the_migration_write_keeps_the_earlier_bytes(self):
        table = self.home / "table.md"
        table.write_text(TABLE_BOTH)
        earlier = dump_file(file_with(judge__mode="each"))
        for target, number, new in (("write", None, None), ("fsync", 1, None), ("replace", None, None), ("fsync", 2, True)):
            with self.subTest(target=target, number=number):
                self.file.write_bytes(earlier)
                patch, _calls = self.inject(target, number)
                with patch:
                    with self.assertRaises(rs.SettingsError) as caught:
                        rs.migrate_teammates(self.home, table, True)
                self.assertEqual(caught.exception.code, "WRITE_FAILED")
                if new is None:
                    self.assertEqual(self.file.read_bytes(), earlier)
                else:
                    self.assertEqual(json.loads(self.file.read_bytes())["judge"]["routes"]["codex"]["step"]["model"],
                                     "gpt-9")
                self.assertEqual(sorted(n for n in self.listing() if n.endswith(".tmp")), [])
                self.assertEqual([n for n in self.listing() if n not in (".settings.lock", "settings.json", "table.md")], [])

    # -- the lock and real processes ----------------------------------------------
    def spawn(self, ready, go, mode, *extra):
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", CHILD, str(SCRIPTS), mode, str(self.home), str(ready), str(go), *extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.child_env())
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def child_env(self):
        return {"PATH": os.environ.get("PATH", ""), "HOME": str(self.safe_home), "PYTHONDONTWRITEBYTECODE": "1"}

    def run_blocked(self, spawn_all, initial, ready_names):
        """Hold the lock, start the children, check they wait, release and wait for them."""
        self.file.write_bytes(initial)
        go = self.home.parent / (self.home.name + "-go")
        self.addCleanup(lambda: go.exists() and go.unlink())
        lock = os.open(self.home / ".settings.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(lambda: os.close(lock) if lock is not None else None)
        fcntl.flock(lock, fcntl.LOCK_EX)
        ready = [self.home.parent / (self.home.name + "-ready-" + name) for name in ready_names]
        processes = spawn_all(ready, go)
        deadline = time.monotonic() + 20
        for path in ready:
            while not path.exists():
                self.assertLess(time.monotonic(), deadline, "a child did not start")
                self.assertIsNone(processes[ready.index(path)].poll())
                time.sleep(0.01)
        go.write_text("go")
        time.sleep(0.5)
        for process in processes:
            self.assertIsNone(process.poll(), "a child finished while the lock was held")
        self.assertEqual(self.file.read_bytes(), initial)
        self.assertEqual([n for n in self.listing() if n.endswith(".tmp")], [])
        fcntl.flock(lock, fcntl.LOCK_UN)
        for process in processes:
            out, err = process.communicate(timeout=30)
            self.assertEqual((process.returncode, out, err), (0, b"", b""), err)
        return json.loads(self.file.read_bytes())

    def test_eight_processes_each_set_another_key(self):
        changes = [("judge.mode", "each"), ("judge.max_fail_rounds", 7), ("judge.deadline_seconds.step", 45),
                   ("judge.deadline_seconds.gate", 3000), ("commit_check", "off"),
                   ("judge.routes.claude.step.model", "m1"), ("judge.routes.codex.gate.effort", "low"),
                   ("judge.routes.claude.gate.model", "m2")]

        def spawn_all(ready, go):
            return [self.spawn(ready[i], go, "update", key, json.dumps(value)) for i, (key, value) in enumerate(changes)]

        result = self.run_blocked(spawn_all, dump_file({"schema_version": 1}), [str(i) for i in range(8)])
        expected = {"schema_version": 1}
        for key, value in changes:
            set_path(expected, key, value)
        self.assertEqual(result, expected)
        self.assertEqual(sorted(self.listing()), [".settings.lock", "settings.json"])
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)

    def test_a_migration_with_write_beside_a_set_keeps_both_results(self):
        table = self.home.parent / (self.home.name + "-table.md")
        table.write_text(TABLE_BOTH)
        self.addCleanup(table.unlink)

        def spawn_all(ready, go):
            return [self.spawn(ready[0], go, "migrate", str(table)),
                    self.spawn(ready[1], go, "update", "judge.mode", json.dumps("each"))]

        result = self.run_blocked(spawn_all, dump_file({"schema_version": 1}), ["migrate", "set"])
        routes = {"codex": {"step": {"model": "gpt-9", "effort": "xhigh"}, "gate": {"model": "gpt-9", "effort": "xhigh"}},
                  "claude": {"step": {"model": "claude-fable-7", "effort": "max"},
                             "gate": {"model": "claude-fable-7", "effort": "max"}}}
        self.assertEqual(result, {"schema_version": 1, "judge": {"routes": routes, "mode": "each"}})
        self.assertEqual(sorted(self.listing()), [".settings.lock", "settings.json"])

    def test_a_home_that_cannot_hold_the_lock_file_is_a_write_failure(self):
        os.chmod(self.home, 0o500)
        self.addCleanup(os.chmod, self.home, 0o700)
        for call in (lambda: rs.update(self.home, "judge.mode", "each"),
                     lambda: rs.migrate_teammates(self.home, self.home / "t.md", True)):
            with self.assertRaises(rs.SettingsError) as caught:
                call()
            self.assertEqual(caught.exception.code, "WRITE_FAILED")
            self.assertTrue(str(caught.exception).startswith("%s/.settings.lock: " % self.home), str(caught.exception))
        self.assertEqual(self.listing(), [])

    def test_the_command_migration_with_write_beside_the_command_set_keeps_both_results(self):
        book = self.home / "logbook"
        folder = self.home / "work" / "plainproject"
        folder.mkdir(parents=True)
        project_home = book / "plainproject"
        project_home.mkdir(parents=True)
        table = self.home / "t.md"
        table.write_text(TABLE_BOTH)
        (project_home / "settings.json").write_bytes(dump_file({"schema_version": 1}))
        before = (project_home / "settings.json").read_bytes()
        lock = os.open(project_home / ".settings.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        base = [sys.executable, "-B", str(SCRIPT), "--host", "claude", "--cwd", str(folder),
                "--registry", str(self.home / "no-registry.json"), "--logbook", str(book)]
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.safe_home), "PYTHONDONTWRITEBYTECODE": "1"}
        children = [subprocess.Popen(base + ["migrate-teammates", "--write", "--table", str(table)], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE),
                    subprocess.Popen(base + ["set", "judge.mode", "each"], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)]
        self.addCleanup(lambda: [c.kill() for c in children if c.poll() is None])
        time.sleep(2)
        for child in children:
            self.assertIsNone(child.poll(), "a command finished while the lock was held")
        self.assertEqual((project_home / "settings.json").read_bytes(), before)
        fcntl.flock(lock, fcntl.LOCK_UN)
        for child in children:
            out, err = child.communicate(timeout=30)
            self.assertEqual((child.returncode, err), (0, b""), err)
            self.assertTrue(out.endswith(b"\n"))
        routes = {"codex": {"step": {"model": "gpt-9", "effort": "xhigh"}, "gate": {"model": "gpt-9", "effort": "xhigh"}},
                  "claude": {"step": {"model": "claude-fable-7", "effort": "max"},
                             "gate": {"model": "claude-fable-7", "effort": "max"}}}
        result = json.loads((project_home / "settings.json").read_bytes())
        self.assertEqual(result, {"schema_version": 1, "judge": {"routes": routes, "mode": "each"}})
        self.assertEqual(sorted(p.name for p in project_home.iterdir()), [".settings.lock", "settings.json"])

    def test_the_lock_is_released_after_a_failure(self):
        with self.assertRaises(rs.SettingsError):
            rs.update(self.home, "judge.mode", "auto")
        lock = os.open(self.home / ".settings.lock", os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock)
        patch, _calls = self.inject("replace", None)
        with patch:
            with self.assertRaises(rs.SettingsError):
                rs.update(self.home, "judge.mode", "each")
        lock = os.open(self.home / ".settings.lock", os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock)

    def test_update_waits_for_a_held_lock_in_process(self):
        lock = os.open(self.home / ".settings.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(BlockingIOError):
                other = os.open(self.home / ".settings.lock", os.O_RDWR)
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(other)
        finally:
            os.close(lock)


# =====================================================================
class World:
    """A registry with an ACTIVE project and a STAGED one, plus a legacy repository, all in a temporary folder."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="readback-settings-world-")
        self.base = Path(self.tmp.name).resolve()
        base = self.base
        self.logbook = base / "logbook"
        self.logbook.mkdir()
        self.registry = self.logbook / ".readback" / "projects.json"
        self.registry.parent.mkdir(mode=0o700)
        self.active_repo = self.git_repo(base / "repos" / "active")
        self.staged_repo = self.git_repo(base / "repos" / "staged")
        self.legacy_repo = self.git_repo(base / "repos" / "legacy")
        self.nohome_repo = self.git_repo(base / "repos" / "nohome")
        self.worktree = base / "trees" / "wt"
        self.worktree.parent.mkdir()
        self.git(self.active_repo, "worktree", "add", "--detach", "--quiet", str(self.worktree), "HEAD")
        self.plain = base / "plain" / "plainfolder"
        self.plain.mkdir(parents=True)
        self.clash = base / "plain" / "clash"
        self.clash.mkdir()
        self.retired = base / "retired-root"
        self.retired.mkdir()
        self.reserved = self.logbook / "clash"
        self.reserved.mkdir()
        self.marked = base / "marked"
        self.marked.mkdir()
        self.centered = base / "centered"
        self.centered.mkdir()
        self.active_home = self.logbook / "active-project"
        self.staged_home = self.logbook / "staged-project"
        self.legacy_home = self.logbook / "legacy"
        self.plain_home = self.logbook / "plainfolder"
        self.legacy_home.mkdir()
        self.plain_home.mkdir()
        self.active_uuid, self.active_repo_uuid, self.active = self.project(
            self.active_home, self.active_repo, "ACTIVE", [str(self.retired), str(self.reserved)])
        _unused, _unused2, self.staged = self.project(self.staged_home, self.staged_repo, "STAGED", [])
        pr.init_registry(self.registry)
        pr.apply_registry(self.registry, {"registry_version": 1, "generation": 0,
                                          "projects": [self.active, self.staged]}, 0)
        marker = {"marker_version": 1, "project_uuid": str(uuid.uuid4()), "repo_uuid": str(uuid.uuid4())}
        (self.marked / pr.REPOSITORY_MARKER).write_text(json.dumps(marker))
        (self.centered / "project.json").write_text(json.dumps({"project_uuid": str(uuid.uuid4())}))
        self.broken_registry = base / "broken-registry.json"
        self.broken_registry.write_text("not a registry")

    def git(self, path, *arguments):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false",
             "-c", "user.name=Fixture Author", "-c", "user.email=fixture@example.invalid", "-C", str(path), *arguments],
            env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    def git_repo(self, path):
        path.mkdir(parents=True)
        self.git(path, "init", "-q")
        self.git(path, "commit", "--allow-empty", "-q", "-m", "Synthetic fixture")
        return path

    @staticmethod
    def identity(path):
        info = path.stat()
        return {"device": info.st_dev, "inode": info.st_ino}

    def project(self, home, repo, state, retired):
        home.mkdir(parents=True)
        profile = {"profile_version": 1, "profile_id": "sample-v1", "protocol_version": "0.7",
                   "record_types": ["DEC", "CON", "HND", "LOG"], "core_language": "en",
                   "extension_namespace": "sample", "extensions": {"sample": {"storage": "markdown-single-writer"}}}
        (home / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
        digest = hashlib.sha256((home / "profile.json").read_bytes()).hexdigest()
        project_uuid, home_uuid, repo_uuid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        (home / "project.json").write_text(json.dumps({
            "project_version": 1, "project_uuid": project_uuid, "home_uuid": home_uuid, "protocol_version": "0.7",
            "profile_version": 1, "profile_sha256": digest, "activation_state": state}, indent=2) + "\n")
        common = Path(self.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if state == "ACTIVE":
            (repo / pr.REPOSITORY_MARKER).write_text(json.dumps({
                "marker_version": 1, "project_uuid": project_uuid, "repo_uuid": repo_uuid}))
        entry = {"project_uuid": project_uuid, "slug": home.name, "home": str(home),
                 "home_identity": self.identity(home), "home_uuid": home_uuid, "protocol_version": "0.7",
                 "profile_version": 1, "activation_state": state, "profile_sha256": digest,
                 "repositories": [{"repo_uuid": repo_uuid, "root": str(repo), "root_identity": self.identity(repo),
                                   "git_common_dir": str(common), "git_common_dir_identity": self.identity(common),
                                   "worktrees": []}], "retired_roots": retired}
        return project_uuid, repo_uuid, entry

    def close(self):
        self.tmp.cleanup()


WORLD = None


def world():
    global WORLD
    if WORLD is None:
        WORLD = World()
    return WORLD


def tearDownModule():
    if WORLD is not None:
        WORLD.close()


TABLE_CLAUDE_ONLY = "| judge | claude | codex | gpt-9 | xhigh |\n"
TABLE_CODEX_ONLY = "| judge | codex | claude | claude-fable-7 | max |\n"
TABLE_LEGACY_CODEX_ROW = "| judge | codex | gpt-8 | low |\n"      # a legacy row that applies to host claude
TABLE_LEGACY_CLAUDE_ROW = "| judge | claude | fable | medium |\n"  # a legacy row that applies to host codex
TABLE_NEITHER = "| role | host | provider | model | effort |\n| lead | claude | claude | fable | high |\n"


def route_keys(provider, model, effort):
    out = {}
    for tier in ("step", "gate"):
        out["judge.routes.%s.%s.model" % (provider, tier)] = model
        out["judge.routes.%s.%s.effort" % (provider, tier)] = effort
    return out


class CommandTests(Guarded):
    """ACC-3: the command as a process, and D1 resolution."""

    @classmethod
    def setUpClass(cls):
        cls.w = world()

    def setUp(self):
        super().setUp()
        w = self.w
        for home in (w.active_home, w.legacy_home, w.plain_home, w.logbook / "nohome"):
            for name in ("settings.json", ".settings.lock"):
                if (home / name).exists():
                    (home / name).unlink()
        if (w.logbook / "nohome").exists():
            (w.logbook / "nohome").rmdir()
        self.tables = tempfile.TemporaryDirectory(prefix="readback-settings-tables-")
        self.addCleanup(self.tables.cleanup)
        self.tdir = Path(self.tables.name).resolve()

    def table(self, text, name="TEAMMATES.md"):
        path = self.tdir / name
        path.write_text(text)
        return path

    def run_cmd(self, *args, cwd=None, home=None, env=None):
        w = self.w
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(home if home else self.safe_home),
                       "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        environment.update(env or {})
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *map(str, args)], cwd=str(cwd or w.base),
                              env=environment, capture_output=True, timeout=30)

    def common(self, host="claude"):
        return ["--host", host, "--registry", self.w.registry, "--logbook", self.w.logbook]

    def ok(self, proc, stdout):
        self.assertEqual(proc.stderr, b"")
        self.assertEqual(proc.stdout, stdout)
        self.assertEqual(proc.returncode, 0)

    def err(self, proc, code, message, returncode=3):
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.stderr, jbytes({"status": code, "error": message}))
        self.assertEqual(proc.returncode, returncode)

    def usage(self, proc, *needles):
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(proc.stderr.startswith(b"usage: readback_settings.py"), proc.stderr)
        self.assertTrue(proc.stderr.endswith(b"\n"))
        for needle in needles:
            self.assertIn(needle.encode(), proc.stderr)

    def show_bytes(self, home, status, source, canon, settings):
        return jbytes({"home": str(home), "status": status, "source": source, "digest": sha(canon),
                       "settings": settings})

    # -- show ------------------------------------------------------------------
    def test_show_a_legacy_project_without_a_file(self):
        w = self.w
        for host in ("claude", "codex"):
            for cwd in (w.legacy_repo, self.make_sub(w.legacy_repo)):
                with self.subTest(host=host, cwd=str(cwd)):
                    proc = self.run_cmd(*self.common(host), "--cwd", cwd, "show")
                    self.ok(proc, self.show_bytes(w.legacy_home, "LEGACY_FALLBACK", "defaults", DEF_CANON, defaults()))
        self.assertEqual(entries(w.legacy_home), [])
        proc = self.run_cmd(*self.common(), "--cwd", w.plain, "show")
        self.ok(proc, self.show_bytes(w.plain_home, "LEGACY_FALLBACK", "defaults", DEF_CANON, defaults()))
        self.assertEqual(entries(w.plain_home), [])

    def make_sub(self, repo):
        sub = repo / "nested" / "résumé"
        sub.mkdir(parents=True, exist_ok=True)
        return sub

    def test_show_a_legacy_project_with_a_file(self):
        w = self.w
        raw = dump_file(file_with(judge__mode="each"))
        (w.legacy_home / "settings.json").write_bytes(raw)
        expected = with_version(defaults())
        expected["judge"]["mode"] = "each"
        proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "show")
        self.ok(proc, self.show_bytes(w.legacy_home, "LEGACY_FALLBACK", "file", MODE_EACH_CANON, expected))
        self.assertEqual((w.legacy_home / "settings.json").read_bytes(), raw)
        self.assertEqual(entries(w.legacy_home), ["settings.json"])

    def test_show_a_project_with_an_active_registry_binding(self):
        w = self.w
        raw = dump_file(ALL_FILE)
        (w.active_home / "settings.json").write_bytes(raw)
        for cwd in (w.active_repo, self.make_sub(w.active_repo), w.active_home, w.worktree):
            if cwd == w.worktree:
                continue
            for host in ("claude", "codex"):
                with self.subTest(cwd=str(cwd), host=host):
                    proc = self.run_cmd(*self.common(host), "--cwd", cwd, "show")
                    self.ok(proc, self.show_bytes(w.active_home, "ACTIVE", "file", ALL_CANON, ALL_FILE))
        self.assertEqual((w.active_home / "settings.json").read_bytes(), raw)

    def test_the_output_form(self):
        w = self.w
        proc = self.run_cmd(*self.common(), "--cwd", w.active_repo, "show")
        self.assertTrue(proc.stdout.endswith(b"}\n"))
        self.assertEqual(proc.stdout.count(b"\n"), 1)
        self.assertEqual(list(json.loads(proc.stdout)), ["home", "status", "source", "digest", "settings"])
        self.assertEqual(proc.stderr, b"")

    def test_the_default_folder_is_the_folder_of_the_process(self):
        w = self.w
        sub = self.make_sub(w.active_repo)
        proc = self.run_cmd("--host", "claude", "--registry", w.registry, "--logbook", w.logbook, "show", cwd=sub)
        self.ok(proc, self.show_bytes(w.active_home, "ACTIVE", "defaults", DEF_CANON, defaults()))
        proc = self.run_cmd("--host", "claude", "--registry", w.registry, "--logbook", w.logbook, "show", cwd=w.legacy_repo)
        self.ok(proc, self.show_bytes(w.legacy_home, "LEGACY_FALLBACK", "defaults", DEF_CANON, defaults()))
        proc = self.run_cmd("--host", "claude", "--registry", w.registry, "--logbook", w.logbook,
                            "set", "judge.mode", "each", cwd=w.legacy_repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((w.legacy_home / "settings.json").read_bytes(),
                         dump_file(file_with(judge__mode="each")))
        self.assertFalse((w.active_home / "settings.json").exists())

    def test_the_default_registry_and_logbook_come_from_the_home_of_the_user(self):
        w = self.w
        before = snapshot(w.base)
        self.addCleanup(lambda: self.assertEqual(snapshot(w.base), before))
        proc = self.run_cmd("--host", "claude", "show", cwd=w.active_repo, home=w.base)
        self.ok(proc, self.show_bytes(w.active_home, "ACTIVE", "defaults", DEF_CANON, defaults()))
        proc = self.run_cmd("--host", "claude", "show", cwd=w.legacy_repo, home=w.base)
        self.ok(proc, self.show_bytes(w.legacy_home, "LEGACY_FALLBACK", "defaults", DEF_CANON, defaults()))
        other = self.tdir / "other-logbook"
        proc = self.run_cmd("--host", "claude", "--logbook", other, "show", cwd=w.legacy_repo, home=w.base)
        self.ok(proc, self.show_bytes(other / "legacy", "LEGACY_FALLBACK", "defaults", DEF_CANON, defaults()))
        proc = self.run_cmd("--host", "claude", "--registry", self.tdir / "none.json", "show", cwd=w.legacy_repo,
                            home=w.base)
        self.ok(proc, self.show_bytes(w.legacy_home, "LEGACY_FALLBACK", "defaults", DEF_CANON, defaults()))
        proc = self.run_cmd("--host", "claude", "--registry", self.tdir / "none.json", "show", cwd=w.active_repo,
                            home=w.base)
        self.err(proc, "UNREGISTERED_REPOSITORY",
                 "Marked repository has no matching registered location; rebind explicitly")

    def test_show_reports_an_invalid_file(self):
        w = self.w
        (w.legacy_home / "settings.json").write_bytes(dump_file(file_with(judge__mode="auto")))
        proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "show")
        self.err(proc, "INVALID_SETTINGS", "%s: judge.mode: invalid value" % (w.legacy_home / "settings.json"))
        (w.legacy_home / "settings.json").write_bytes(b"{")
        proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "show")
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.returncode, 3)
        body = json.loads(proc.stderr)
        self.assertEqual(body["status"], "INVALID_SETTINGS")
        self.assertTrue(body["error"].startswith("%s: not valid JSON: " % (w.legacy_home / "settings.json")))
        self.assertEqual(list(body), ["status", "error"])

    def test_show_in_a_project_whose_home_is_missing_prints_defaults(self):
        w = self.w
        proc = self.run_cmd(*self.common(), "--cwd", w.nohome_repo, "show")
        self.ok(proc, self.show_bytes(w.logbook / "nohome", "LEGACY_FALLBACK", "defaults", DEF_CANON, defaults()))
        self.assertFalse((w.logbook / "nohome").exists())

    # -- set ---------------------------------------------------------------------
    def test_set_a_string_value(self):
        w = self.w
        expected = with_version(defaults())
        expected["judge"]["mode"] = "each"
        proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "set", "judge.mode", "each")
        self.ok(proc, self.show_bytes(w.legacy_home, "LEGACY_FALLBACK", "file", MODE_EACH_CANON, expected))
        self.assertEqual((w.legacy_home / "settings.json").read_bytes(),
                         b'{\n  "schema_version": 1,\n  "judge": {\n    "mode": "each"\n  }\n}\n')
        self.assertEqual(stat.S_IMODE((w.legacy_home / "settings.json").stat().st_mode), 0o600)
        self.assertEqual(entries(w.legacy_home), [".settings.lock", "settings.json"])

    def test_set_an_integer_value(self):
        w = self.w
        proc = self.run_cmd(*self.common(), "--cwd", w.active_repo, "set", "judge.max_fail_rounds", "7")
        self.assertEqual(proc.stderr, b"")
        self.assertEqual(proc.returncode, 0)
        body = json.loads(proc.stdout)
        self.assertIs(type(body["settings"]["judge"]["max_fail_rounds"]), int)
        self.assertEqual(body["settings"]["judge"]["max_fail_rounds"], 7)
        self.assertEqual((w.active_home / "settings.json").read_bytes(),
                         b'{\n  "schema_version": 1,\n  "judge": {\n    "max_fail_rounds": 7\n  }\n}\n')
        proc = self.run_cmd(*self.common(), "--cwd", w.active_repo, "set", "judge.deadline_seconds.gate", "0045")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads((w.active_home / "settings.json").read_bytes()),
                         {"schema_version": 1, "judge": {"max_fail_rounds": 7, "deadline_seconds": {"gate": 45}}})
        self.assertIn(b'"gate": 45', proc.stdout)

    def test_set_prints_what_show_prints_afterwards(self):
        w = self.w
        proc = self.run_cmd(*self.common(), "--cwd", w.active_repo, "set", "judge.deadline_seconds.step", "30")
        after = self.run_cmd(*self.common("codex"), "--cwd", w.active_repo, "show")
        self.assertEqual(proc.stdout, after.stdout)
        expected = with_version(defaults())
        expected["judge"]["deadline_seconds"]["step"] = 30
        self.assertEqual(json.loads(proc.stdout)["settings"], expected)
        canon = DEF_V1_CANON.replace('"step":300', '"step":30')
        self.assertEqual(proc.stdout, self.show_bytes(w.active_home, "ACTIVE", "file", canon, expected))

    def test_set_every_leaf_of_the_schema(self):
        w = self.w
        values = {"judge.mode": "off", "judge.provider": "opposite", "commit_check": "off", "judge.max_fail_rounds": "10",
                  "judge.deadline_seconds.step": "3600", "judge.deadline_seconds.gate": "30"}
        for key in LEAF_KEYS:
            with self.subTest(key):
                if (w.legacy_home / "settings.json").exists():
                    (w.legacy_home / "settings.json").unlink()
                value = values.get(key, "v-1")
                proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "set", key, value)
                self.assertEqual((proc.stderr, proc.returncode), (b"", 0), proc.stderr)
                expected = {"schema_version": 1}
                set_path(expected, key, int(value) if value.isdigit() else value)
                self.assertEqual(json.loads((w.legacy_home / "settings.json").read_bytes()), expected)

    def test_set_keeps_the_other_keys(self):
        w = self.w
        (w.legacy_home / "settings.json").write_bytes(dump_file(ALL_FILE))
        proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "set", "judge.routes.codex.step.model", "gpt-new")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = copy.deepcopy(ALL_FILE)
        expected["judge"]["routes"]["codex"]["step"]["model"] = "gpt-new"
        self.assertEqual((w.legacy_home / "settings.json").read_bytes(), dump_file(expected))
        self.assertEqual(json.loads(proc.stdout)["settings"], expected)

    def test_set_with_a_value_that_is_not_a_number_is_a_usage_error(self):
        w = self.w
        for value in ("abc", "", "-3", "3.5", "1e3", "+4", " 4", "4 ", "3\n", "٣", "0x10", "true"):
            with self.subTest(value):
                proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "set", "judge.max_fail_rounds", value)
                self.usage(proc, "the value for judge.max_fail_rounds must be a whole number")
                self.assertEqual(entries(w.legacy_home), [])
        proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "set", "judge.deadline_seconds.step", "x")
        self.usage(proc, "the value for judge.deadline_seconds.step must be a whole number")

    def test_set_with_a_number_out_of_range_is_an_invalid_settings_error(self):
        w = self.w
        path = w.legacy_home / "settings.json"
        for key, value in (("judge.max_fail_rounds", "11"), ("judge.max_fail_rounds", "0"),
                           ("judge.max_fail_rounds", "9" * 5000), ("judge.deadline_seconds.step", "29"),
                           ("judge.deadline_seconds.gate", "3601"), ("judge.deadline_seconds.gate", "000")):
            with self.subTest(key=key, value=value[:6]):
                proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "set", key, value)
                self.err(proc, "INVALID_SETTINGS", "%s: %s: invalid value" % (path, key))
                self.assertEqual(entries(w.legacy_home), [".settings.lock"])
                self.assertFalse(path.exists())

    def test_set_a_string_value_out_of_range_changes_nothing(self):
        w = self.w
        path = w.legacy_home / "settings.json"
        raw = dump_file(file_with(judge__mode="each"))
        path.write_bytes(raw)
        for key, value in (("judge.mode", "auto"), ("judge.provider", "same"), ("commit_check", "enforce"),
                           ("judge.routes.claude.step.model", "a b"), ("judge.routes.codex.gate.effort", "x.y"),
                           ("judge.routes.codex.gate.model", "_x")):
            with self.subTest(key):
                proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "set", key, value)
                self.err(proc, "INVALID_SETTINGS", "%s: %s: invalid value" % (path, key))
                self.assertEqual(path.read_bytes(), raw)

    def test_set_with_a_key_that_is_not_a_leaf_is_a_usage_error(self):
        w = self.w
        for key in ("judge", "judge.routes", "judge.routes.claude", "judge.routes.claude.step", "judge.deadline_seconds",
                    "judge.nope", "nope", "judge.mode.x", "schema_version", "", "judge.", ".judge", "Judge.mode"):
            with self.subTest(key):
                proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "set", key, "5")
                self.usage(proc, "the key must name a leaf of the schema: judge.mode, judge.provider,")
                self.assertEqual(entries(w.legacy_home), [])

    def test_set_fails_when_the_home_folder_is_missing(self):
        w = self.w
        proc = self.run_cmd(*self.common(), "--cwd", w.nohome_repo, "set", "judge.mode", "each")
        self.err(proc, "HOME_MISSING", "%s: the project home folder does not exist" % (w.logbook / "nohome"))
        self.assertFalse((w.logbook / "nohome").exists())

    def test_set_with_an_invalid_existing_file_changes_nothing(self):
        w = self.w
        path = w.legacy_home / "settings.json"
        path.write_bytes(dump_file({"schema_version": 1, "other": 1}))
        proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "set", "judge.mode", "each")
        self.err(proc, "INVALID_SETTINGS", "%s: other: unknown key" % path)
        self.assertEqual(path.read_bytes(), dump_file({"schema_version": 1, "other": 1}))

    # -- usage errors -------------------------------------------------------------
    def test_write_is_accepted_by_the_migration_only(self):
        w = self.w
        for args in (["show", "--write"], ["--write", "show"], ["set", "judge.mode", "each", "--write"],
                     ["set", "--write", "judge.mode", "each"], ["--write", "migrate-teammates"]):
            with self.subTest(args):
                proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, *args)
                self.usage(proc, "unrecognized arguments: --write")
                self.assertEqual(entries(w.legacy_home), [])

    def test_a_missing_host_is_a_usage_error_that_says_the_host_is_needed(self):
        w = self.w
        for args in (["show"], ["set", "judge.mode", "each"], ["migrate-teammates"], ["migrate-teammates", "--write"]):
            with self.subTest(args):
                proc = self.run_cmd("--registry", w.registry, "--logbook", w.logbook, "--cwd", w.legacy_repo, *args)
                self.usage(proc, "the host is needed: pass --host claude or --host codex")
                self.assertEqual(entries(w.legacy_home), [])

    def test_other_usage_errors_exit_with_2(self):
        w = self.w
        cases = [(["--host", "other", "show"], "invalid choice: 'other'"), (["--host", "claude"], "required: command"),
                 ([], "required: command"), (["--host", "claude", "frobnicate"], "invalid choice: 'frobnicate'"),
                 (["--host", "claude", "set", "judge.mode"], "the following arguments are required: value"),
                 (["--host", "claude", "set"], "the following arguments are required: key, value"),
                 (["--host", "claude", "show", "extra"], "unrecognized arguments: extra"),
                 (["--host", "claude", "--cw", str(w.legacy_repo), "show"], "invalid choice: '%s'" % w.legacy_repo),
                 (["--host", "claude", "migrate-teammates", "--wri"], "unrecognized arguments: --wri"),
                 (["--host", "claude", "--nope", "show"], "unrecognized arguments: --nope")]
        for args, needle in cases:
            with self.subTest(args):
                proc = self.run_cmd(*args, cwd=w.legacy_repo)
                self.usage(proc, needle)

    # -- D1 errors: one code each, and no file is written ----------------------------
    def d1_cases(self):
        w = self.w
        version = self.tdir / "version-registry.json"
        os.chmod(self.tdir, 0o700)
        version.write_text(json.dumps({"registry_version": 2, "generation": 0, "projects": []}))
        mini = self.tdir / "mini"
        mini.mkdir()
        repo = w.git_repo(mini / "repo")
        entry = w.project(mini / "home", repo, "ACTIVE", [])[2]
        registry = mini / "registry" / "projects.json"
        registry.parent.mkdir(mode=0o700)
        pr.init_registry(registry)
        pr.apply_registry(registry, {"registry_version": 1, "generation": 0, "projects": [entry]}, 0)
        with open(mini / "home" / "profile.json", "a") as handle:  # the profile no longer matches its registered digest
            handle.write("\n")
        marker_version = self.tdir / "marker-version"
        marker_version.mkdir()
        (marker_version / pr.REPOSITORY_MARKER).write_text(json.dumps({
            "marker_version": 2, "project_uuid": str(uuid.uuid4()), "repo_uuid": str(uuid.uuid4())}))
        return [
            ("UNSUPPORTED_VERSION", dict(cwd=w.legacy_repo), {"registry": version}),
            ("UNSUPPORTED_VERSION", dict(cwd=marker_version), {}),
            ("IDENTITY_MISMATCH", dict(cwd=repo), {"registry": registry}),
            ("INVALID", dict(cwd=w.base / "missing-folder"), {}),
            ("INVALID", dict(cwd=w.legacy_repo), {"registry": w.broken_registry}),
            ("INACTIVE", dict(cwd=w.staged_repo), {}),
            ("INACTIVE", dict(cwd=w.staged_home), {}),
            ("RETIRED", dict(cwd=w.retired), {}),
            ("UNREGISTERED_WORKTREE", dict(cwd=w.worktree), {}),
            ("UNREGISTERED_REPOSITORY", dict(cwd=w.marked), {}),
            ("UNREGISTERED_CENTER", dict(cwd=w.centered), {}),
            ("RESERVED_PATH", dict(cwd=w.clash), {}),
        ]

    def test_each_resolver_error_is_an_error_and_writes_no_file(self):
        w = self.w
        table = self.table(TABLE_BOTH)
        for code, where, extra in self.d1_cases():
            cwd = where["cwd"]
            registry = extra.get("registry", w.registry)
            try:
                pr.resolve_project(registry, cwd, "claude", logbook=w.logbook, legacy_fallback=True)
                self.fail("the resolver accepted " + str(cwd))
            except pr.RegistryError as exc:
                self.assertEqual(exc.code, code)
                message = str(exc)
            for command in (["show"], ["set", "judge.mode", "each"], ["migrate-teammates", "--table", table],
                            ["migrate-teammates", "--write", "--table", table]):
                with self.subTest(code=code, cwd=str(cwd), command=command[0] + str(len(command))):
                    before = (snapshot(w.base), snapshot(self.tdir))
                    proc = self.run_cmd("--host", "claude", "--registry", registry, "--logbook", w.logbook,
                                        "--cwd", cwd, *command)
                    self.err(proc, code, message)
                    self.assertEqual((snapshot(w.base), snapshot(self.tdir)), before)

    def test_resolve_returns_the_documented_object(self):
        w = self.w
        sub = self.make_sub(w.active_repo)
        common = str(w.active_repo / ".git")
        got = rs.resolve(sub, "claude", w.registry, w.logbook)
        self.assertEqual(got, {"status": "ACTIVE", "home": str(w.active_home), "project_uuid": w.active_uuid,
                               "repo_uuid": w.active_repo_uuid, "worktree_root": str(w.active_repo),
                               "git_common_dir": common, "repository": w.active_repo_uuid})
        self.assertEqual(rs.resolve(sub, "codex", w.registry, w.logbook), got)
        got = rs.resolve(w.active_home, "claude", w.registry, w.logbook)
        self.assertEqual(got, {"status": "ACTIVE", "home": str(w.active_home), "project_uuid": w.active_uuid,
                               "repo_uuid": None, "worktree_root": None, "git_common_dir": None,
                               "repository": str(w.active_home)})
        got = rs.resolve(w.legacy_repo, "claude", w.registry, w.logbook)
        self.assertEqual(got, {"status": "LEGACY_FALLBACK", "home": str(w.legacy_home), "project_uuid": None,
                               "repo_uuid": None, "worktree_root": str(w.legacy_repo),
                               "git_common_dir": str(w.legacy_repo / ".git"),
                               "repository": str(w.legacy_repo / ".git")})
        got = rs.resolve(w.plain, "claude", w.registry, w.logbook)
        self.assertEqual(got, {"status": "LEGACY_FALLBACK", "home": str(w.plain_home), "project_uuid": None,
                               "repo_uuid": None, "worktree_root": None, "git_common_dir": None,
                               "repository": str(w.plain)})
        alias = self.tdir / "alias"
        alias.symlink_to(w.plain, target_is_directory=True)
        self.assertEqual(rs.resolve(alias, "claude", w.registry, w.logbook)["repository"], str(w.plain))
        self.assertEqual(rs.resolve(str(w.plain), "claude", str(w.registry), str(w.logbook))["repository"], str(w.plain))

    def test_a_linked_worktree_of_a_legacy_repository_shares_the_repository_identity(self):
        w = self.w
        tree = w.base / "trees" / "legacy-wt"
        w.git(w.legacy_repo, "worktree", "add", "--detach", "--quiet", str(tree), "HEAD")
        self.addCleanup(lambda: w.git(w.legacy_repo, "worktree", "remove", "--force", str(tree)))
        got = rs.resolve(tree, "claude", w.registry, w.logbook)
        self.assertEqual(got["worktree_root"], str(tree))
        self.assertEqual(got["git_common_dir"], str(w.legacy_repo / ".git"))
        self.assertEqual(got["repository"], str(w.legacy_repo / ".git"))
        self.assertEqual(got["home"], str(w.logbook / "legacy-wt"))

    def test_resolve_defaults_come_from_the_home_of_the_user(self):
        w = self.w
        before = snapshot(w.base)
        self.addCleanup(lambda: self.assertEqual(snapshot(w.base), before))
        with mock.patch.dict(os.environ, {"HOME": str(w.base)}):
            self.assertEqual(rs.resolve(w.active_repo, "claude")["status"], "ACTIVE")
            self.assertEqual(rs.resolve(w.legacy_repo, "claude")["home"], str(w.legacy_home))
            self.assertEqual(rs.resolve(w.legacy_repo, "claude", registry=w.base / "none.json")["status"],
                             "LEGACY_FALLBACK")
            with self.assertRaises(rs.SettingsError) as caught:  # the marked repository needs its registry
                rs.resolve(w.active_repo, "claude", registry=w.base / "none.json")
            self.assertEqual(caught.exception.code, "UNREGISTERED_REPOSITORY")
            other = self.tdir / "book"
            self.assertEqual(rs.resolve(w.legacy_repo, "claude", logbook=other)["home"], str(other / "legacy"))

    def test_a_status_other_than_active_or_legacy_fallback_is_an_error(self):
        w = self.w
        for status in ("STAGED", "INACTIVE", "RETIRED", "", "active"):
            with self.subTest(status):
                with mock.patch.object(pr, "resolve_project", return_value={"status": status}):
                    with self.assertRaises(rs.SettingsError) as caught:
                        rs.resolve(w.legacy_repo, "claude", w.registry, w.logbook)
                self.assertEqual(caught.exception.code, status)
                self.assertEqual(str(caught.exception), "the project is not usable: " + status)

    def test_resolve_calls_the_resolver_with_the_legacy_fallback(self):
        w = self.w
        real = pr.resolve_project
        with mock.patch.object(pr, "resolve_project", wraps=real) as spy:
            rs.resolve(w.legacy_repo, "codex", w.registry, w.logbook)
        spy.assert_called_once_with(w.registry, w.legacy_repo, "codex", logbook=w.logbook, legacy_fallback=True)
        with mock.patch.dict(os.environ, {"HOME": str(self.safe_home)}):
            with mock.patch.object(pr, "resolve_project", wraps=real) as spy:
                with self.assertRaises(rs.SettingsError):  # the empty home has no registry and the folder is marked
                    rs.resolve(w.active_repo, "codex")
        spy.assert_called_once_with(self.safe_home / "logbook" / ".readback" / "projects.json", w.active_repo, "codex",
                                    logbook=self.safe_home / "logbook", legacy_fallback=True)

    def test_a_registry_error_becomes_a_settings_error_with_its_code(self):
        w = self.w
        with mock.patch.object(pr, "resolve_project", side_effect=pr.RegistryError("BUSY", "the registry is busy")):
            with self.assertRaises(rs.SettingsError) as caught:
                rs.resolve(w.legacy_repo, "claude", w.registry, w.logbook)
        self.assertEqual((caught.exception.code, str(caught.exception)), ("BUSY", "the registry is busy"))

    # -- migrate-teammates ---------------------------------------------------------
    def migrate(self, text, *args, cwd=None, host="claude", extra=None):
        table = self.table(text)
        return table, self.run_cmd(*self.common(host), "--cwd", cwd or self.w.legacy_repo, "migrate-teammates",
                                   "--table", table, *args)

    def mapping_both(self):
        return {**route_keys("codex", "gpt-9", "xhigh"), **route_keys("claude", "claude-fable-7", "max")}

    def test_migration_without_write_prints_the_mapping_and_writes_nothing(self):
        w = self.w
        cases = [(TABLE_BOTH, self.mapping_both()),
                 (TABLE_CLAUDE_ONLY, route_keys("codex", "gpt-9", "xhigh")),
                 (TABLE_CODEX_ONLY, route_keys("claude", "claude-fable-7", "max")),
                 (TABLE_LEGACY_CODEX_ROW, route_keys("codex", "gpt-8", "low")),
                 (TABLE_LEGACY_CLAUDE_ROW, route_keys("claude", "fable", "medium")),
                 (TABLE_NEITHER, {}), ("", {})]
        for text, mapping in cases:
            for host in ("claude", "codex"):
                with self.subTest(text[:40], host=host):
                    table, proc = self.migrate(text, host=host)
                    self.ok(proc, jbytes({"table": str(table), "mapping": mapping, "written": []}))
                    self.assertEqual(entries(w.legacy_home), [])
        proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "migrate-teammates", "--table", self.tdir / "absent.md")
        self.ok(proc, jbytes({"table": str(self.tdir / "absent.md"), "mapping": {}, "written": []}))

    def test_migration_key_order_is_the_claude_host_first(self):
        table, proc = self.migrate(TABLE_CODEX_ONLY + TABLE_CLAUDE_ONLY)
        keys = list(json.loads(proc.stdout)["mapping"])
        self.assertEqual(keys[:4], ["judge.routes.codex.step.model", "judge.routes.codex.step.effort",
                                    "judge.routes.codex.gate.model", "judge.routes.codex.gate.effort"])
        self.assertEqual(keys[4:], ["judge.routes.claude.step.model", "judge.routes.claude.step.effort",
                                    "judge.routes.claude.gate.model", "judge.routes.claude.gate.effort"])

    def test_migration_with_write_creates_the_file(self):
        w = self.w
        cases = [(TABLE_BOTH, self.mapping_both()),
                 (TABLE_CLAUDE_ONLY, route_keys("codex", "gpt-9", "xhigh")),
                 (TABLE_CODEX_ONLY, route_keys("claude", "claude-fable-7", "max")),
                 (TABLE_LEGACY_CODEX_ROW, route_keys("codex", "gpt-8", "low")),
                 (TABLE_LEGACY_CLAUDE_ROW, route_keys("claude", "fable", "medium"))]
        for text, mapping in cases:
            with self.subTest(text[:40]):
                if (w.legacy_home / "settings.json").exists():
                    (w.legacy_home / "settings.json").unlink()
                table, proc = self.migrate(text, "--write")
                self.ok(proc, jbytes({"table": str(table), "mapping": mapping, "written": list(mapping)}))
                expected = {"schema_version": 1}
                for key, value in mapping.items():
                    set_path(expected, key, value)
                self.assertEqual((w.legacy_home / "settings.json").read_bytes(), dump_file(expected))
                self.assertEqual(stat.S_IMODE((w.legacy_home / "settings.json").stat().st_mode), 0o600)
                self.assertEqual(entries(w.legacy_home), [".settings.lock", "settings.json"])

    def test_migration_with_write_and_nothing_to_write_creates_no_file(self):
        w = self.w
        for text in (TABLE_NEITHER, ""):
            with self.subTest(text[:10]):
                table, proc = self.migrate(text, "--write")
                self.ok(proc, jbytes({"table": str(table), "mapping": {}, "written": []}))
                self.assertFalse((w.legacy_home / "settings.json").exists())
        proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "migrate-teammates", "--write",
                            "--table", self.tdir / "absent.md")
        self.ok(proc, jbytes({"table": str(self.tdir / "absent.md"), "mapping": {}, "written": []}))
        self.assertFalse((w.legacy_home / "settings.json").exists())

    def test_migration_keeps_an_existing_model_and_an_existing_effort_each_on_its_own(self):
        w = self.w
        path = w.legacy_home / "settings.json"
        mapping = self.mapping_both()
        base = {"schema_version": 1}
        for key, value in mapping.items():
            set_path(base, key, value)
        cases = [
            ("a model only", file_with(judge__routes__codex__step__model="keep-m"),
             {"judge.routes.codex.step.model": "keep-m"}),
            ("an effort only", file_with(judge__routes__codex__gate__effort="keep-e"),
             {"judge.routes.codex.gate.effort": "keep-e"}),
            ("a model and an effort of other leaves", file_with(judge__routes__claude__step__model="k1",
                                                                 judge__routes__claude__gate__effort="k2"),
             {"judge.routes.claude.step.model": "k1", "judge.routes.claude.gate.effort": "k2"}),
            ("the model and the effort of one tier", file_with(judge__routes__codex__step={"model": "k3", "effort": "k4"}),
             {"judge.routes.codex.step.model": "k3", "judge.routes.codex.step.effort": "k4"}),
            ("everything", base, mapping),
        ]
        for label, existing, kept in cases:
            with self.subTest(label):
                path.write_bytes(dump_file(existing))
                table, proc = self.migrate(TABLE_BOTH, "--write")
                expected = copy.deepcopy(existing)
                written = []
                for key, value in mapping.items():
                    if key in kept:
                        set_path(expected, key, kept[key])
                    elif not self.has(existing, key):
                        set_path(expected, key, value)
                        written.append(key)
                self.ok(proc, jbytes({"table": str(table), "mapping": mapping, "written": written}))
                self.assertEqual(path.read_bytes(), dump_file(expected))
                for key, value in kept.items():
                    node = json.loads(path.read_bytes())
                    for part in key.split("."):
                        node = node[part]
                    self.assertEqual(node, value)
        path.write_bytes(dump_file(base))
        before = path.read_bytes()
        os.utime(path, (1, 1))
        table, proc = self.migrate(TABLE_BOTH, "--write")
        self.ok(proc, jbytes({"table": str(table), "mapping": mapping, "written": []}))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime, 1)

    @staticmethod
    def has(obj, dotted):
        for part in dotted.split("."):
            if type(obj) is not dict or part not in obj:
                return False
            obj = obj[part]
        return True

    def test_migration_keeps_other_settings(self):
        w = self.w
        path = w.legacy_home / "settings.json"
        path.write_bytes(dump_file(file_with(judge__mode="each", commit_check="off", judge__max_fail_rounds=5)))
        table, proc = self.migrate(TABLE_CLAUDE_ONLY, "--write")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = file_with(judge__mode="each", commit_check="off", judge__max_fail_rounds=5)
        for key, value in route_keys("codex", "gpt-9", "xhigh").items():
            set_path(expected, key, value)
        self.assertEqual(path.read_bytes(), dump_file(expected))

    def test_migration_in_a_project_with_an_active_binding(self):
        w = self.w
        table = self.table(TABLE_BOTH)
        proc = self.run_cmd(*self.common(), "--cwd", w.active_repo, "migrate-teammates", "--table", table, "--write")
        mapping = self.mapping_both()
        self.ok(proc, jbytes({"table": str(table), "mapping": mapping, "written": list(mapping)}))
        expected = {"schema_version": 1}
        for key, value in mapping.items():
            set_path(expected, key, value)
        self.assertEqual((w.active_home / "settings.json").read_bytes(), dump_file(expected))
        self.assertFalse((w.legacy_home / "settings.json").exists())
        self.assertEqual(json.loads(self.run_cmd(*self.common(), "--cwd", w.active_repo, "show").stdout)["source"], "file")

    def test_migration_without_write_in_a_project_with_an_active_binding(self):
        w = self.w
        table = self.table(TABLE_BOTH)
        for host in ("claude", "codex"):
            proc = self.run_cmd(*self.common(host), "--cwd", w.active_repo, "migrate-teammates", "--table", table)
            self.ok(proc, jbytes({"table": str(table), "mapping": self.mapping_both(), "written": []}))
        self.assertEqual(entries(w.active_home), ["profile.json", "project.json"])

    def test_a_table_that_select_route_rejects_gives_exit_3_and_no_write(self):
        w = self.w
        bad = [
            ("| judge | codex | claude | gpt-4 | high |\n", "the Claude judge must use the Fable family"),
            ("| judge | claude | claude | fable | high |\n", "the matching judge row must name the opposite provider"),
            ("| judge | claude | codex | bad model | high |\n", "the matching judge row has an invalid model"),
            ("| judge | claude | codex | gpt-9 | h!gh |\n", "the matching judge row has an invalid effort"),
            ("| judge | other | codex | gpt-9 | high |\n", "the judge row names an unknown host"),
            ("| judge | other | gpt-9 | high |\n", "the legacy judge row names an unknown provider"),
            ("| judge | codex | claude | high |\n", "the host judge row is missing its effort"),
            ("| judge | claude |\n", "the matching judge row needs four or five columns"),
            ("| judge | codex | claude | gpt-4 | high |\n" + TABLE_CLAUDE_ONLY, "the Claude judge must use the Fable family"),
        ]
        existing = dump_file(file_with(judge__mode="each"))
        for text, reason in bad:
            for write in ([], ["--write"]):
                for have_file in (False, True):
                    with self.subTest(text[:50], write=write, have_file=have_file):
                        path = w.legacy_home / "settings.json"
                        if path.exists():
                            path.unlink()
                        if have_file:
                            path.write_bytes(existing)
                        table, proc = self.migrate(text, *write)
                        self.assertEqual(proc.stdout, b"")
                        self.assertEqual(proc.returncode, 3)
                        body = json.loads(proc.stderr)
                        self.assertEqual(list(body), ["status", "error"])
                        self.assertEqual(body["status"], "INVALID_TABLE")
                        self.assertEqual(body["error"], "%s: %s" % (table, reason))
                        self.assertEqual(path.exists(), have_file)
                        if have_file:
                            self.assertEqual(path.read_bytes(), existing)
                        self.assertEqual([n for n in entries(w.legacy_home) if n != "settings.json"], [])

    def test_an_unreadable_table_gives_exit_3(self):
        w = self.w
        folder = self.tdir / "a-folder"
        folder.mkdir()
        proc = self.run_cmd(*self.common(), "--cwd", w.legacy_repo, "migrate-teammates", "--write", "--table", folder)
        self.err(proc, "INVALID_TABLE", "%s: cannot read the teammates table" % folder)
        self.assertEqual(entries(w.legacy_home), [])

    def test_a_migration_with_an_invalid_existing_file_changes_nothing(self):
        w = self.w
        path = w.legacy_home / "settings.json"
        for raw in (dump_file({"schema_version": 1, "other": 1}), b"not json", dump_file(file_with(judge__mode="auto"))):
            with self.subTest(raw):
                path.write_bytes(raw)
                table, proc = self.migrate(TABLE_BOTH, "--write")
                self.assertEqual(proc.stdout, b"")
                self.assertEqual(proc.returncode, 3)
                self.assertEqual(json.loads(proc.stderr)["status"], "INVALID_SETTINGS")
                self.assertTrue(json.loads(proc.stderr)["error"].startswith(str(path) + ": "))
                self.assertEqual(path.read_bytes(), raw)
                table, proc = self.migrate(TABLE_BOTH)
                self.ok(proc, jbytes({"table": str(table), "mapping": self.mapping_both(), "written": []}))

    def test_a_migration_with_a_missing_home_fails_only_with_write(self):
        w = self.w
        table = self.table(TABLE_BOTH)
        proc = self.run_cmd(*self.common(), "--cwd", w.nohome_repo, "migrate-teammates", "--table", table)
        self.ok(proc, jbytes({"table": str(table), "mapping": self.mapping_both(), "written": []}))
        proc = self.run_cmd(*self.common(), "--cwd", w.nohome_repo, "migrate-teammates", "--table", table, "--write")
        self.err(proc, "HOME_MISSING", "%s: the project home folder does not exist" % (w.logbook / "nohome"))
        self.assertFalse((w.logbook / "nohome").exists())

    def test_the_table_comes_from_the_option_then_the_environment_then_the_home_folder(self):
        w = self.w
        default_table = w.base / "logbook" / "TEAMMATES.md"
        default_table.write_text(TABLE_CLAUDE_ONLY)
        self.addCleanup(default_table.unlink)
        before = snapshot(w.base)

        def only_the_intended_files():
            added = set(snapshot(w.base)) - set(before)
            self.assertEqual(added, {str(w.legacy_home / "settings.json"), str(w.legacy_home / ".settings.lock")})
        self.addCleanup(only_the_intended_files)
        env_table = self.table(TABLE_CODEX_ONLY, "env.md")
        option_table = self.table(TABLE_LEGACY_CODEX_ROW, "option.md")
        base = [*self.common(), "--cwd", w.legacy_repo, "migrate-teammates"]
        proc = self.run_cmd(*base, home=w.base)
        self.ok(proc, jbytes({"table": str(default_table), "mapping": route_keys("codex", "gpt-9", "xhigh"), "written": []}))
        proc = self.run_cmd(*base, home=w.base, env={"READBACK_TEAMMATES": str(env_table)})
        self.ok(proc, jbytes({"table": str(env_table), "mapping": route_keys("claude", "claude-fable-7", "max"),
                              "written": []}))
        proc = self.run_cmd(*base, "--table", option_table, home=w.base, env={"READBACK_TEAMMATES": str(env_table)})
        self.ok(proc, jbytes({"table": str(option_table), "mapping": route_keys("codex", "gpt-8", "low"), "written": []}))
        proc = self.run_cmd(*base, "--write", home=w.base, env={"READBACK_TEAMMATES": str(env_table)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads((w.legacy_home / "settings.json").read_bytes())["judge"]["routes"],
                         {"claude": {"step": {"model": "claude-fable-7", "effort": "max"},
                                     "gate": {"model": "claude-fable-7", "effort": "max"}}})
        proc = self.run_cmd(*base, home=w.base, env={"READBACK_TEAMMATES": ""})
        self.assertEqual(json.loads(proc.stdout)["table"], str(default_table))
        proc = self.run_cmd("--host", "claude", "--registry", w.registry, "--logbook", w.logbook,
                            "--cwd", w.legacy_repo, "migrate-teammates", home=self.safe_home)
        self.ok(proc, jbytes({"table": str(self.safe_home / "logbook" / "TEAMMATES.md"), "mapping": {}, "written": []}))

    def test_a_settings_error_prints_one_json_object_to_stderr(self):
        w = self.w
        proc = self.run_cmd(*self.common(), "--cwd", w.base / "missing-folder", "show")
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.returncode, 3)
        self.assertTrue(proc.stderr.endswith(b"}\n"))
        self.assertEqual(proc.stderr.count(b"\n"), 1)
        self.assertEqual(list(json.loads(proc.stderr)), ["status", "error"])

    def test_non_ascii_paths_are_printed_as_utf8(self):
        w = self.w
        repo = w.git_repo(w.base / "repos" / "résumé")
        home = w.logbook / "résumé"
        home.mkdir()
        proc = self.run_cmd(*self.common(), "--cwd", repo, "show")
        self.ok(proc, self.show_bytes(home, "LEGACY_FALLBACK", "defaults", DEF_CANON, defaults()))
        self.assertIn("résumé".encode("utf-8"), proc.stdout)
        home.rmdir()
        proc = self.run_cmd(*self.common(), "--cwd", repo, "set", "judge.mode", "each")
        self.err(proc, "HOME_MISSING", "%s: the project home folder does not exist" % home)
        self.assertIn("résumé".encode("utf-8"), proc.stderr)


# =====================================================================
class Unsupported(Exception):
    pass


ANNOTATIONS = {"$schema", "$id", "title", "description", "examples", "$defs"}
SUPPORTED = ANNOTATIONS | {"type", "properties", "required", "additionalProperties", "enum", "const", "pattern",
                           "minimum", "maximum", "$ref"}


def keywords_used(node, found=None):
    """Every schema keyword in a schema, without reading property names or example values as keywords."""
    found = set() if found is None else found
    if type(node) is not dict:
        return found
    for key, value in node.items():
        found.add(key)
        if key in ("properties", "$defs"):
            for sub in value.values():
                keywords_used(sub, found)
        elif key == "additionalProperties" and type(value) is dict:
            keywords_used(value, found)
    return found


def same(a, b):
    """JSON equality: a boolean is not a number."""
    return type(a) is type(b) and a == b


def is_integer(value):
    """JSON Schema: an integer is a number without a fraction, so 3.0 counts. A boolean is not a number."""
    return type(value) is int or (type(value) is float and value.is_integer())


def validate(schema, instance, root=None, trail=""):
    """A small independent validator. Returns a list of (dotted path, reason). Rejects an unknown keyword."""
    root = schema if root is None else root
    errors = []
    for key in schema:
        if key not in SUPPORTED:
            raise Unsupported("keyword not supported: " + key)
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        errors += validate(target, instance, root, trail)
    if "type" in schema:
        kind = schema["type"]
        valid = {"object": type(instance) is dict, "integer": is_integer(instance), "string": type(instance) is str}
        if kind not in valid:
            raise Unsupported("type not supported: " + kind)
        if not valid[kind]:
            return errors + [(trail, "type")]
    if "const" in schema and not same(instance, schema["const"]):
        errors.append((trail, "const"))
    if "enum" in schema and not any(same(instance, item) for item in schema["enum"]):
        errors.append((trail, "enum"))
    if "pattern" in schema:
        pattern = schema["pattern"]
        if pattern.endswith("$"):
            pattern = pattern[:-1] + r"\Z"  # in JSON Schema a "$" matches only at the very end
        if type(instance) is not str or re.search(pattern, instance) is None:
            errors.append((trail, "pattern"))
    if "minimum" in schema and (not is_integer(instance) or instance < schema["minimum"]):
        errors.append((trail, "minimum"))
    if "maximum" in schema and (not is_integer(instance) or instance > schema["maximum"]):
        errors.append((trail, "maximum"))
    if type(instance) is dict:
        for name in schema.get("required", []):
            if name not in instance:
                errors.append((join(trail, name), "required"))
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for name, value in instance.items():
            if name in properties:
                errors += validate(properties[name], value, root, join(trail, name))
            elif extra is False:
                errors.append((join(trail, name), "additionalProperties"))
    return errors


def join(trail, name):
    return trail + "." + name if trail else name


def schema_text():
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


class SchemaTests(Guarded):
    """ACC-4: the schema file, an independent validator, the loader on missing, malformed and BOM files."""

    def setUp(self):
        super().setUp()
        self.schema = schema_text()
        self.home = temp_home(self)

    def test_the_schema_file_is_valid_json(self):
        raw = SCHEMA.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(raw.endswith(b"\n"))
        self.assertIs(type(json.loads(raw.decode("utf-8"))), dict)
        self.assertEqual(self.schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(self.schema["$id"], "urn:readback:settings:1")
        self.assertEqual(self.schema["type"], "object")
        self.assertEqual(self.schema["required"], ["schema_version"])
        self.assertIs(self.schema["additionalProperties"], False)
        self.assertEqual(self.schema["properties"]["schema_version"], {"const": 1})
        self.assertEqual(sorted(self.schema["properties"]), ["commit_check", "judge", "schema_version"])

    def test_the_validator_supports_every_keyword_the_file_uses(self):
        used = keywords_used(self.schema)
        self.assertTrue(used <= SUPPORTED, sorted(used - SUPPORTED))
        for keyword in ("type", "properties", "required", "additionalProperties", "enum", "const", "pattern",
                        "minimum", "maximum", "$ref", "$defs", "examples"):
            self.assertIn(keyword, used)
        self.assertEqual(validate(self.schema, {"schema_version": 1}), [])

    def test_the_validator_rejects_a_keyword_it_does_not_support(self):
        for keyword, value in (("minLength", 1), ("oneOf", []), ("format", "x"), ("items", {}), ("anyOf", []),
                               ("allOf", []), ("not", {}), ("if", {}), ("maxLength", 3), ("multipleOf", 5),
                               ("exclusiveMinimum", 1), ("dependentRequired", {}), ("patternProperties", {}),
                               ("propertyNames", {}), ("default", 1)):
            with self.subTest(keyword):
                with self.assertRaises(Unsupported):
                    validate({"type": "object", keyword: value}, {})
                with self.assertRaises(Unsupported):
                    validate({"properties": {"a": {keyword: value}}}, {"a": 1})
        with self.assertRaises(Unsupported):
            validate({"type": "array"}, [])
        self.assertEqual(keywords_used({"properties": {"type": {"type": "string"}, "pattern": {"minimum": 1}},
                                        "examples": [{"enum": 1}]}), {"properties", "type", "minimum", "examples"})

    def test_the_validator_judges_each_keyword(self):
        self.assertEqual(validate({"type": "integer"}, True), [("", "type")])
        self.assertEqual(validate({"type": "integer"}, 1.5), [("", "type")])
        self.assertEqual(validate({"type": "integer"}, 4.0), [])
        self.assertEqual(validate({"minimum": 2}, 1.0), [("", "minimum")])
        self.assertEqual(validate({"maximum": 2}, 3.0), [("", "maximum")])
        self.assertEqual(validate({"minimum": 2, "maximum": 3}, 2.0), [])
        self.assertEqual(validate({"type": "integer"}, 4), [])
        self.assertEqual(validate({"type": "string"}, 4), [("", "type")])
        self.assertEqual(validate({"const": 1}, True), [("", "const")])
        self.assertEqual(validate({"const": 1}, 1), [])
        self.assertEqual(validate({"enum": ["a", 2]}, True), [("", "enum")])
        self.assertEqual(validate({"enum": ["a", 2]}, 2), [])
        self.assertEqual(validate({"pattern": "^a+$"}, "aa\n"), [("", "pattern")])
        self.assertEqual(validate({"pattern": "^a+$"}, "aa"), [])
        self.assertEqual(validate({"pattern": "^a+$"}, "ba"), [("", "pattern")])
        self.assertEqual(validate({"minimum": 2, "maximum": 3}, 1), [("", "minimum")])
        self.assertEqual(validate({"minimum": 2, "maximum": 3}, 4), [("", "maximum")])
        self.assertEqual(validate({"minimum": 2, "maximum": 3}, 3), [])
        self.assertEqual(validate({"minimum": 2}, True), [("", "minimum")])
        self.assertEqual(validate({"required": ["a"]}, {}), [("a", "required")])
        self.assertEqual(validate({"properties": {"a": {}}, "additionalProperties": False}, {"b": 1}),
                         [("b", "additionalProperties")])
        self.assertEqual(validate({"properties": {"a": {"properties": {"b": {"type": "string"}}}}}, {"a": {"b": 1}}),
                         [("a.b", "type")])
        self.assertEqual(validate({"$defs": {"x": {"type": "string"}}, "properties": {"a": {"$ref": "#/$defs/x"}}},
                                  {"a": 1}), [("a", "type")])

    def test_each_instance_of_d9_has_the_stated_outcome_in_the_validator_and_the_module(self):
        seen = 0
        for label, raw, expected, obj in CASES:
            with self.subTest(label):
                if raw is None:
                    continue
                try:
                    instance = json.loads(raw.decode("utf-8"))
                    parses = True
                except ValueError:
                    parses = False
                (self.home / "settings.json").write_bytes(raw)
                if parses:
                    seen += 1
                    errors = validate(self.schema, instance)
                    self.assertEqual(not errors, expected[0] == "ok", (label, errors))
                    if expected[0] == "error" and expected[1] not in (None, "$"):
                        self.assertIn(expected[1], [path for path, _reason in errors], (label, errors))
                try:
                    rs.load(self.home)
                    module_ok = True
                except rs.SettingsError:
                    module_ok = False
                self.assertEqual(module_ok, expected[0] == "ok")
        self.assertGreater(seen, 40)

    def test_the_f1_file_has_no_instance_and_f2_validates(self):
        self.assertEqual(validate(self.schema, {"schema_version": 1}), [])
        labels = {label for label, _raw, _expected, _obj in CASES}
        for label in ("F1", "F2", "F3", "F4", "F5 both leaves", "F5 model only", "F6", "F7", "F8", "F9", "F10",
                      "F12 0", "F12 11", "F13 model with a space", "F13 empty effort", "F14 missing", "F14 two",
                      "F14 true", "F15 not JSON", "F15 list", "F16 model fabé", "F16 byte order mark"):
            self.assertIn(label, labels)
        for tier in ("step", "gate"):
            for value in (29, 3601, True, "300"):
                self.assertIn("F11 %s %r" % (tier, value), labels)
        for place in OBJECT_PLACES:
            for value in (None, 5, "x", [], [1]):
                self.assertIn("F17 %s=%r" % (place, value), labels)

    def test_boundaries_agree_between_the_validator_and_the_module(self):
        for key, value, valid in BOUNDARIES:
            with self.subTest(key=key, value=value):
                instance = set_path({"schema_version": 1}, key, value)
                errors = validate(self.schema, instance)
                self.assertEqual(not errors, valid, errors)
                (self.home / "settings.json").write_bytes(json.dumps(instance).encode("utf-8"))
                try:
                    rs.load(self.home)
                    module_ok = True
                except rs.SettingsError:
                    module_ok = False
                self.assertEqual(module_ok, valid)
                if not valid:
                    self.assertEqual(errors[0][0], key)

    def test_integral_floats_are_valid_in_the_schema_and_invalid_in_the_module(self):
        """The one stated difference: JSON Schema counts 300.0 as an integer, the loader accepts an int only."""
        for key, value in (("judge.deadline_seconds.step", 300.0), ("judge.deadline_seconds.gate", 30.0),
                           ("judge.deadline_seconds.gate", 3600.0), ("judge.max_fail_rounds", 3.0),
                           ("judge.max_fail_rounds", 1.0), ("judge.max_fail_rounds", 10.0)):
            with self.subTest(key=key, value=value):
                instance = set_path({"schema_version": 1}, key, value)
                self.assertEqual(validate(self.schema, instance), [])
                (self.home / "settings.json").write_bytes(json.dumps(instance).encode())
                with self.assertRaises(rs.SettingsError) as caught:
                    rs.load(self.home)
                self.assertEqual(str(caught.exception), "%s: %s: invalid value" % (self.home / "settings.json", key))

    def test_unknown_keys_are_rejected_at_every_level_by_both(self):
        for place in ["", *OBJECT_PLACES]:
            with self.subTest(place):
                instance = {"schema_version": 1}
                if place:
                    set_path(instance, place + ".unknown", 1)
                else:
                    instance["unknown"] = 1
                errors = validate(self.schema, instance)
                self.assertEqual(errors, [((place + "." if place else "") + "unknown", "additionalProperties")])
                (self.home / "settings.json").write_bytes(json.dumps(instance).encode())
                with self.assertRaises(rs.SettingsError) as caught:
                    rs.load(self.home)
                self.assertEqual(str(caught.exception).split(": ", 1)[1],
                                 (place + "." if place else "") + "unknown: unknown key")

    def test_the_defaults_object_of_the_module_equals_the_example_of_the_schema_file(self):
        examples = self.schema["examples"]
        self.assertEqual(len(examples), 1)
        self.assertEqual(rs.DEFAULTS, examples[0])
        self.assertEqual(examples[0], defaults())
        self.assertEqual(rs.canonical(examples[0]), DEF_CANON.encode())
        self.assertEqual(validate(self.schema, examples[0]), [])
        self.assertEqual(examples[0]["schema_version"], 1)
        self.assertEqual(validate(self.schema, {"schema_version": 1, **examples[0]}), [])
        (self.home / "settings.json").write_bytes(json.dumps({"schema_version": 1, **examples[0]}).encode())
        self.assertEqual(rs.load(self.home)["settings"], {"schema_version": 1, **examples[0]})

    def test_the_schema_limits_equal_the_documented_limits(self):
        defs = self.schema["$defs"]
        judge = self.schema["properties"]["judge"]["properties"]
        self.assertEqual(judge["mode"], {"enum": ["off", "gate", "each"]})
        self.assertEqual(judge["provider"], {"const": "opposite"})
        self.assertEqual(self.schema["properties"]["commit_check"], {"enum": ["off", "observe"]})
        self.assertEqual(judge["max_fail_rounds"], {"type": "integer", "minimum": 1, "maximum": 10})
        self.assertEqual(defs["deadline"], {"type": "integer", "minimum": 30, "maximum": 3600})
        self.assertEqual(judge["deadline_seconds"]["properties"],
                         {"step": {"$ref": "#/$defs/deadline"}, "gate": {"$ref": "#/$defs/deadline"}})
        self.assertEqual(defs["route"]["properties"]["model"]["pattern"], "^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
        self.assertEqual(defs["route"]["properties"]["effort"]["pattern"], "^[A-Za-z0-9_-]+$")
        self.assertEqual(sorted(judge["routes"]["properties"]), ["claude", "codex"])

    # -- the loader: a missing file, malformed JSON and the byte order mark -------------
    def test_loader_a_missing_file(self):
        loaded = rs.load(self.home)
        self.assertEqual(loaded["source"], "defaults")
        self.assertEqual(loaded["settings"], defaults())
        self.assertEqual(loaded["path"], str(self.home / "settings.json"))
        self.assertEqual(loaded["digest"], sha(DEF_CANON))
        self.assertEqual(entries(self.home), [])

    def test_loader_malformed_json(self):
        for raw in (b"{", b"settings", b'{"schema_version": 1,}', b"", b"{'schema_version': 1}"):
            with self.subTest(raw):
                (self.home / "settings.json").write_bytes(raw)
                with self.assertRaises(rs.SettingsError) as caught:
                    rs.load(self.home)
                self.assertEqual(caught.exception.code, "INVALID_SETTINGS")
                self.assertTrue(str(caught.exception).startswith(str(self.home / "settings.json") + ": not valid JSON: "))

    def test_loader_byte_order_mark(self):
        for body in (b'{"schema_version": 1}', b"{}", b"[]"):
            with self.subTest(body):
                (self.home / "settings.json").write_bytes(b"\xef\xbb\xbf" + body)
                with self.assertRaises(rs.SettingsError) as caught:
                    rs.load(self.home)
                self.assertEqual(caught.exception.code, "INVALID_SETTINGS")
                self.assertTrue(str(caught.exception).startswith(
                    str(self.home / "settings.json") + ": not valid JSON: Unexpected UTF-8 BOM"))
        (self.home / "settings.json").write_bytes(b'{"schema_version": 1}')
        self.assertEqual(rs.load(self.home)["source"], "file")


if __name__ == "__main__":
    unittest.main(failfast=True)
