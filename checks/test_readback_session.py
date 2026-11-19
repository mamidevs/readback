#!/usr/bin/env python3
"""Offline checks for scripts/readback_session.py.

Run one class: python3 -B checks/test_readback_session.py StateTests
Run all:       python3 -B checks/test_readback_session.py

StateTests and AuthorizationTests call the module in this process, each with its own temporary `base`
folder, a 20 second alarm (so that a lock that waits forever fails the test) and no real owner folder.
ConcurrencyTests starts real processes (subprocess, python -c) that import the module, wait for a start
line on stdin and call one function; a lock held by this process (fcntl.flock on the lock file) shows that
each mutation waits for it. Injected failures replace the module-level names `_write` and `_rename`: in
this process for an exception, and in a driver process for a crash (os._exit) between the temporary write
and the rename. Every process run has a timeout; a run that exceeds it is killed and the test fails.

The run stops at the first failure so that a mutation run is fast.
"""

import copy
import errno
import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import readback_session as rs  # noqa: E402

AT = "2026-11-17T10:20:30Z"
AT2 = "2026-11-17T10:20:31Z"
SID = "sess-1"
TARGET = {"project_home": "/home/p", "repository": "/repo/r", "lineage": "lineage-1", "tier": "step",
          "settings_digest": "digest-1"}
TARGET_NAMES = ("project_home", "repository", "lineage", "tier", "settings_digest")
DIFFERENT = {"project_home": "/home/q", "repository": "/repo/s", "lineage": "lineage-2", "tier": "gate",
             "settings_digest": "digest-2"}
SEVEN = ("event_id", "at") + TARGET_NAMES


def target(**changes):
    return dict(TARGET, **changes)


def authorization(event_id, at=AT, **changes):
    return dict(target(**changes), event_id=event_id, at=at)


def state_of(host="claude", sid=SID, mode=None, mode_event=None, authorizations=(), processed=()):
    return {"schema_version": 1, "host": host, "session_id": sid, "mode": mode, "mode_event": mode_event,
            "authorizations": list(authorizations), "processed_events": list(processed)}


def good_state(host="claude", sid=SID):
    return state_of(host, sid, "gate", {"id": "m1", "at": AT}, [authorization("e1")], ["e1", "e0"])


def jbytes(value):
    return json.dumps(value).encode("utf-8")


# ----------------------------------------------------------------- the invalid files

def _edited(*path, value=None, delete=False):
    state = copy.deepcopy(good_state())
    node = state
    for key in path[:-1]:
        node = node[key]
    if delete:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return jbytes(state)


BAD_TIMES = [
    "2026-11-17T10:20:30", "2026-11-17T10:20:30+00:00", "2026-11-17 10:20:30Z", "2026-11-17T10:20:30z",
    "2026-11-17T10:20:30.5Z", "2026-11-17T10:20:30Z\n", " 2026-11-17T10:20:30Z",
    "２０２６-11-17T10:20:30Z", "2026-13-17T10:20:30Z", "2026-00-17T10:20:30Z",
    "2026-02-30T10:20:30Z", "2026-02-29T10:20:30Z", "2026-11-00T10:20:30Z", "2026-11-31T10:20:30Z",
    "2026-11-17T24:00:00Z", "2026-11-17T10:60:00Z", "2026-11-17T10:20:60Z", "0000-01-01T00:00:00Z", "",
    5, None, ["x"], "2026-1-17T10:20:30Z", "2026-11-17T10:20:3Z",
]
GOOD_TIMES = ["2024-02-29T23:59:59Z", "0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z", "2026-11-17T00:00:00Z",
              "2026-12-31T23:59:59Z", "2026-01-01T00:00:00Z"]


def invalid_cases():
    """(label, raw bytes, reason) for the file claude-sess-1.json."""
    cases = []

    def add(label, raw, reason):
        cases.append((label, raw, reason))

    for label, raw in (("empty", b""), ("brace", b"{"), ("half", b'{"a": '), ("bad utf-8", b"\xff\xfe\x00{}"),
                       ("text", b"not json")):
        add("not JSON " + label, raw, "not JSON")
    # Some JSON decoders can parse this depth. The decoded array is still an invalid session.
    deep_array = b"[" * 50000 + b"]" * 50000
    try:
        json.loads(deep_array)
    except RecursionError:
        deep_reason = "not JSON"
    else:
        deep_reason = "not an object"
    add("deep array", deep_array, deep_reason)
    for label, raw in (("list", b"[]"), ("null", b"null"), ("string", b'"x"'), ("number", b"3"), ("true", b"true"),
                       ("state in list", b"[" + jbytes(good_state()) + b"]")):
        add("not an object " + label, raw, "not an object")
    for key in rs.STATE_KEYS:
        add("missing key " + key, _edited(key, delete=True), "missing key " + key)
    add("unknown key", _edited("extra", value=1), "unknown key extra")
    for value in (2, True, 1.0, "1", None, 0, [1]):
        add("schema_version %r" % (value,), _edited("schema_version", value=value), "schema_version")
    for value in ("codex", 5, None, ""):
        add("host %r" % (value,), _edited("host", value=value), "host")
    for value in ("other", 5, None, "", "sess-1 "):
        add("session_id %r" % (value,), _edited("session_id", value=value), "session_id")
    for value in ("bad", "OFF", 0, False, [], {}, "", True):
        add("mode %r" % (value,), _edited("mode", value=value), "mode")
    for value in ([], {}, "x", 5, {"id": "e"}, {"at": AT}, {"id": "e", "at": AT, "extra": 1},
                  {"id": "", "at": AT}, {"id": 5, "at": AT}, {"id": None, "at": AT}, [["id", "at"]]):
        add("mode_event %r" % (value,), _edited("mode_event", value=value), "mode_event")
    for value in BAD_TIMES:
        add("mode_event at %r" % (value,), _edited("mode_event", "at", value=value), "mode_event")
        add("authorization at %r" % (value,), _edited("authorizations", 0, "at", value=value),
            "authorizations[0]")
    for value in ({}, "x", None, 5, True):
        add("authorizations %r" % (value,), _edited("authorizations", value=value), "authorizations")
    for value in ([1], [None], [[]], ["x"], [{}]):
        add("authorization item %r" % (value,), _edited("authorizations", value=value), "authorizations[0]")
    state = good_state()
    state["authorizations"].append("x")
    add("second authorization bad", jbytes(state), "authorizations[1]")
    for key in SEVEN:
        add("authorization missing " + key, _edited("authorizations", 0, key, delete=True), "authorizations[0]")
    add("authorization extra key", _edited("authorizations", 0, "extra", value="x"), "authorizations[0]")
    for value in ("bad", "Step", "", 5, None, ["step"]):
        add("tier %r" % (value,), _edited("authorizations", 0, "tier", value=value), "authorizations[0]")
    for value in ("", 5, None, ["e"]):
        add("authorization event_id %r" % (value,), _edited("authorizations", 0, "event_id", value=value),
            "authorizations[0]")
    for key in ("project_home", "repository", "lineage", "settings_digest"):
        for value in (5, None, ["x"], {"a": "b"}, True):
            add("%s %r" % (key, value), _edited("authorizations", 0, key, value=value), "authorizations[0]")
    for value in ({}, "x", None, 5, True):
        add("processed_events %r" % (value,), _edited("processed_events", value=value), "processed_events")
    for value in ([5], [""], [None], ["e1", 2], [["e"]], ["e1", ""]):
        add("processed_events items %r" % (value,), _edited("processed_events", value=value), "processed_events")
    return cases


INVALID = invalid_cases()
ALL_IDS_SAFE = ["a", "Z", "0", "a.b", "a_b", "a-b", "a.", "a-", "a_", "A" + "b" * 127, "9abc.def_ghi-jkl",
                "0123456789abcdef-0123-4567"]
UNSAFE_IDS = ["", None, 5, 1.5, b"abc", ["a"], True, ".a", "-a", "_a", "a/b", "/a", "a b", " a", "a ", "é",
              "aé", "ａ", "a" * 129, "a" * 1000, "../a", "a/../b", "a\n", "\na", "a\x00b", "a\\b", "a:b",
              "a*", "a~", "a b"]


# ----------------------------------------------------------------- helpers

def snapshot(folder):
    """name -> (kind, bytes of a regular file, mode, inode, mtime_ns) for each entry of a folder."""
    result = {}
    for name in sorted(os.listdir(folder)):
        info = os.lstat(os.path.join(folder, name))
        data = None
        if stat.S_ISREG(info.st_mode):
            with open(os.path.join(folder, name), "rb") as handle:
                data = handle.read()
        result[name] = (stat.S_IFMT(info.st_mode), data, stat.S_IMODE(info.st_mode), info.st_ino, info.st_mtime_ns)
    return result


def without_locks(snap):
    return {name: value for name, value in snap.items() if not name.endswith(".lock")}


def modes_below(root):
    """relative path -> permission bits for the folder and everything below it."""
    result = {".": stat.S_IMODE(os.lstat(root).st_mode)}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(folder, name)
            result[os.path.relpath(path, root)] = stat.S_IMODE(os.lstat(path).st_mode)
    return result


class WithTemp(unittest.TestCase):
    """A temporary folder, a `base` below it that does not exist yet, and a 20 second alarm."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="readback-session-")
        self.addCleanup(temp.cleanup)
        self.root = os.path.realpath(temp.name)
        self.base = os.path.join(self.root, "sessions")
        home = tempfile.TemporaryDirectory(prefix="readback-session-home-")
        self.addCleanup(home.cleanup)
        self.home = os.path.realpath(home.name)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home})  # a call that ignores `base` lands here
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._home_is_empty)
        signal.signal(signal.SIGALRM, self._alarm)
        signal.alarm(20)
        self.addCleanup(signal.alarm, 0)

    def _home_is_empty(self):
        self.assertEqual(os.listdir(self.home), [], "a call wrote below HOME although the test gave a base")

    @staticmethod
    def lock_is_held(lock_path):
        """True when another open file description holds the flock on the lock file."""
        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    @staticmethod
    def _alarm(signum, frame):
        raise AssertionError("the call did not return within 20 seconds")

    def path(self, host="claude", sid=SID):
        return os.path.join(self.base, host + "-" + sid + ".json")

    def lock_path(self, host="claude", sid=SID):
        return os.path.join(self.base, host + "-" + sid + ".lock")

    def put(self, raw, host="claude", sid=SID):
        os.makedirs(self.base, exist_ok=True)
        with open(self.path(host, sid), "wb") as handle:
            handle.write(raw)

    def raw(self, host="claude", sid=SID):
        with open(self.path(host, sid), "rb") as handle:
            return handle.read()

    def assert_unchanged(self, before):
        """The folder holds the same entries, bytes, modes, inodes and times, lock files apart."""
        self.assertEqual(without_locks(snapshot(self.base)), without_locks(before))

    def read(self, host="claude", sid=SID):
        return rs.read(host, sid, self.base)

    def set_mode(self, mode, event, at=AT, host="claude", sid=SID):
        return rs.set_mode(host, sid, mode, event, at, self.base)

    def authorize(self, event, at=AT, host="claude", sid=SID, **changes):
        fields = target(**changes)
        return rs.authorize(host, sid, event, at, fields["project_home"], fields["repository"], fields["lineage"],
                            fields["tier"], fields["settings_digest"], self.base)

    def consume(self, host="claude", sid=SID, **changes):
        fields = target(**changes)
        return rs.consume(host, sid, fields["project_home"], fields["repository"], fields["lineage"], fields["tier"],
                          fields["settings_digest"], self.base)


class SelfChecks:
    """Each comparison helper rejects a difference. Mixed into StateTests and AuthorizationTests."""

    def test_snapshot_rejects_changed_bytes_added_removed_and_rewritten_files(self):
        os.makedirs(self.base)
        file = os.path.join(self.base, "claude-x.json")
        Path(file).write_bytes(b"aaaa")
        before = snapshot(self.base)
        self.assert_unchanged(before)
        Path(file).write_bytes(b"aaab")  # same size, same name
        with self.assertRaises(AssertionError):
            self.assert_unchanged(before)
        Path(file).write_bytes(b"aaaa")
        os.utime(file, ns=(1, 1))
        with self.assertRaises(AssertionError):
            self.assert_unchanged(before)  # the time differs although the bytes are the same again
        before = snapshot(self.base)
        Path(file + ".new").write_bytes(b"aaaa")
        os.rename(file + ".new", file)  # same bytes, other inode
        with self.assertRaises(AssertionError):
            self.assert_unchanged(before)
        os.chmod(file, 0o640)
        before = snapshot(self.base)
        os.chmod(file, 0o600)
        with self.assertRaises(AssertionError):
            self.assert_unchanged(before)
        before = snapshot(self.base)
        Path(self.base, ".tmp-leftover").write_bytes(b"")
        with self.assertRaises(AssertionError):
            self.assert_unchanged(before)
        os.unlink(os.path.join(self.base, ".tmp-leftover"))
        self.assert_unchanged(before)
        Path(self.base, "claude-x.lock").write_bytes(b"")  # a lock file is the one allowed addition
        self.assert_unchanged(before)
        os.unlink(file)
        with self.assertRaises(AssertionError):
            self.assert_unchanged(before)

    def test_lock_probe_tells_a_held_lock_from_a_free_one(self):
        os.makedirs(self.base)
        lock = self.lock_path()
        Path(lock).write_bytes(b"")
        self.assertFalse(self.lock_is_held(lock))
        holder = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            self.assertTrue(self.lock_is_held(lock))
        finally:
            os.close(holder)
        self.assertFalse(self.lock_is_held(lock))

    def test_modes_below_rejects_a_mode_difference(self):
        os.makedirs(os.path.join(self.base, "x"))
        os.chmod(os.path.join(self.base, "x"), 0o755)
        before = modes_below(self.base)
        os.chmod(os.path.join(self.base, "x"), 0o700)
        self.assertNotEqual(modes_below(self.base), before)

    def test_invalid_cases_are_really_invalid_and_the_good_state_is_valid(self):
        self.put(jbytes(good_state()))
        self.assertEqual(self.read(), ("valid", good_state()))
        self.assertGreater(len(INVALID), 150)
        self.assertEqual(len({label for label, _raw, _reason in INVALID}), len(INVALID))


# ----------------------------------------------------------------- ACC-1

class StateTests(SelfChecks, WithTemp):

    # D3 read
    def test_read_absent_without_base_and_with_base_and_creates_nothing(self):
        self.assertEqual(self.read(), ("absent", None))
        self.assertFalse(os.path.exists(self.base))
        os.makedirs(self.base)
        Path(self.base, "canary.txt").write_bytes(b"c")
        before = snapshot(self.base)
        self.assertEqual(self.read(), ("absent", None))
        self.assertEqual(self.read("codex"), ("absent", None))
        self.assertEqual(snapshot(self.base), before)

    def test_read_valid_file_returns_the_whole_state_for_both_hosts(self):
        for host in ("claude", "codex"):
            state = good_state(host)
            self.put(jbytes(state), host)
            self.assertEqual(self.read(host), ("valid", state))
        minimal = state_of()
        self.put(jbytes(minimal))
        self.assertEqual(self.read(), ("valid", minimal))
        full = state_of(mode="each", mode_event={"id": "m", "at": AT},
                        authorizations=[authorization("a"), authorization("b", AT2, tier="gate")],
                        processed=["a", "b", "c"])
        self.put(jbytes(full))
        self.assertEqual(self.read(), ("valid", full))
        self.assertEqual(self.read()[1]["authorizations"][1]["tier"], "gate")

    def test_read_valid_with_every_mode_and_with_exact_calendar_times(self):
        for mode in (None, "off", "gate", "each"):
            self.put(jbytes(state_of(mode=mode)))
            self.assertEqual(self.read(), ("valid", state_of(mode=mode)))
        for value in GOOD_TIMES:
            state = state_of(mode_event={"id": "m", "at": value}, authorizations=[authorization("a", value)])
            self.put(jbytes(state))
            self.assertEqual(self.read(), ("valid", state), value)

    def test_read_each_invalid_file_gives_its_reason_and_changes_nothing(self):
        for label, raw, reason in INVALID:
            self.put(raw)
            before = snapshot(self.base)
            self.assertEqual(self.read(), ("invalid", reason), label)
            self.assertEqual(snapshot(self.base), before, label)

    def test_read_takes_only_the_exact_file_name(self):
        good = jbytes(good_state())
        self.put(jbytes(good_state("codex", SID)), "codex")
        self.put(jbytes(good_state("claude", "sess-10")), "claude", "sess-10")
        Path(self.base, "claude-sess-1.json.bak").write_bytes(good)
        Path(self.base, "claude-sess-1.json~").write_bytes(good)
        Path(self.base, ".tmp-abc").write_bytes(good)
        Path(self.base, ".claude-sess-1.json").write_bytes(good)
        Path(self.base, "claude-sess-1.lock").write_bytes(good)
        os.makedirs(os.path.join(self.base, "sub"))
        Path(self.base, "sub", "claude-sess-1.json").write_bytes(good)
        self.assertEqual(self.read(), ("absent", None))
        self.assertEqual(self.read("codex", "sess-1"), ("valid", good_state("codex", SID)))
        self.assertEqual(self.read("claude", "sess-10"), ("valid", good_state("claude", "sess-10")))

    def test_read_does_not_take_the_state_of_another_host_or_id(self):
        # A file named for another host or id is not read for this one: read gives absent.
        self.put(jbytes(good_state("codex", SID)), "codex", SID)
        self.assertEqual(self.read("claude", SID), ("absent", None))
        self.assertEqual(self.read("codex", SID), ("valid", good_state("codex", SID)))
        self.assertEqual(self.read("codex", "sess-2"), ("absent", None))

    def test_read_not_a_regular_file_and_unreadable(self):
        os.makedirs(os.path.join(self.base, "claude-sess-1.json"))
        self.assertEqual(self.read(), ("invalid", "not a regular file"))
        shutil.rmtree(self.base)
        os.makedirs(self.base)
        Path(self.base, "real.json").write_bytes(jbytes(good_state()))
        os.symlink("real.json", self.path())
        self.assertEqual(self.read(), ("invalid", "not a regular file"))
        os.unlink(self.path())
        os.symlink("nowhere.json", self.path())
        self.assertEqual(self.read(), ("invalid", "not a regular file"))
        os.unlink(self.path())
        self.put(jbytes(good_state()))
        os.chmod(self.path(), 0o000)
        self.assertEqual(self.read(), ("invalid", "unreadable"))
        os.chmod(self.path(), 0o600)
        shutil.rmtree(self.base)
        Path(self.base).write_bytes(b"a file where the folder should be")
        self.assertEqual(self.read(), ("invalid", "unreadable"))

    # D1 ids
    def test_safe_ids_name_the_file_for_both_hosts(self):
        for host in ("claude", "codex"):
            for sid in ALL_IDS_SAFE:
                self.set_mode("each", "m-" + sid, AT, host, sid)
                expected = os.path.join(self.base, host + "-" + sid + ".json")
                self.assertTrue(os.path.isfile(expected), expected)
                self.assertEqual(self.read(host, sid), ("valid", state_of(
                    host, sid, "each", {"id": "m-" + sid, "at": AT}, processed=["m-" + sid])))
                rs.clear(host, sid, self.base)
                self.assertFalse(os.path.exists(expected))
        self.assertEqual(len(ALL_IDS_SAFE[9]), 128)

    def test_unsafe_ids_raise_invalid_id_from_every_function_and_touch_no_file(self):
        calls = {
            "read": lambda host, sid, base: rs.read(host, sid, base),
            "set_mode": lambda host, sid, base: rs.set_mode(host, sid, "off", "e", AT, base),
            "authorize": lambda host, sid, base: rs.authorize(host, sid, "e", AT, "p", "r", "l", "step", "d", base),
            "consume": lambda host, sid, base: rs.consume(host, sid, "p", "r", "l", "step", "d", base),
            "clear": lambda host, sid, base: rs.clear(host, sid, base),
        }
        existing = os.path.join(self.root, "existing")
        os.makedirs(existing)
        Path(existing, "canary.txt").write_bytes(b"c")
        Path(existing, "claude-a.json").write_bytes(jbytes(good_state("claude", "a")))
        before = snapshot(existing)
        for host in ("claude", "codex"):
            for sid in UNSAFE_IDS:
                for name, call in calls.items():
                    with self.assertRaises(rs.SessionError, msg=(host, sid, name)) as caught:
                        call(host, sid, self.base)
                    self.assertEqual(caught.exception.code, "INVALID_ID")
                    self.assertEqual(str(caught.exception), "INVALID_ID")
                    self.assertFalse(os.path.exists(self.base), (host, sid, name))
                    with self.assertRaises(rs.SessionError) as caught:
                        call(host, sid, existing)
                    self.assertEqual(caught.exception.code, "INVALID_ID")
                    self.assertEqual(snapshot(existing), before, (host, sid, name))
        self.assertEqual(sorted(os.listdir(self.root)), ["existing"])

    def test_session_error_codes(self):
        self.assertEqual(rs.INVALID_ID, "INVALID_ID")
        self.assertEqual(rs.INVALID_STATE, "INVALID_STATE")
        self.assertEqual(rs.SessionError("INVALID_STATE").code, "INVALID_STATE")
        self.assertTrue(issubclass(rs.SessionError, Exception))
        self.assertIsNot(rs.SessionError("INVALID_ID").code, None)

    def test_the_default_base_is_below_the_home_folder(self):
        self.assertEqual(rs.DEFAULT_BASE, "~/logbook/.readback/sessions")
        with mock.patch.dict(os.environ, {"HOME": self.root}):
            self.assertEqual(rs.read("claude", SID), ("absent", None))
            self.assertEqual(os.listdir(self.root), [])
            rs.set_mode("claude", SID, "off", "m1", AT)
            expected = os.path.join(self.root, "logbook", ".readback", "sessions", "claude-sess-1.json")
            self.assertTrue(os.path.isfile(expected))
            self.assertEqual(rs.read("claude", SID), ("valid", state_of(
                mode="off", mode_event={"id": "m1", "at": AT}, processed=["m1"])))
            rs.authorize("claude", SID, "e1", AT, "p", "r", "l", "step", "d")
            self.assertEqual(rs.consume("claude", SID, "p", "r", "l", "step", "d"),
                             {"event_id": "e1", "at": AT, "project_home": "p", "repository": "r", "lineage": "l",
                              "tier": "step", "settings_digest": "d"})
            rs.clear("claude", SID)
            self.assertFalse(os.path.exists(expected))
        self.assertEqual(sorted(os.listdir(self.root)), ["logbook"])

    # D9
    def test_session_id_from_env(self):
        self.assertEqual(rs.session_id_from_env("claude", {"CLAUDE_CODE_SESSION_ID": "abc-1"}), "abc-1")
        self.assertEqual(rs.session_id_from_env("codex", {"CODEX_SESSION_ID": "abc-2"}), "abc-2")
        both = {"CLAUDE_CODE_SESSION_ID": "c1", "CODEX_SESSION_ID": "x1"}
        self.assertEqual(rs.session_id_from_env("claude", both), "c1")
        self.assertEqual(rs.session_id_from_env("codex", both), "x1")
        self.assertIsNone(rs.session_id_from_env("claude", {"CODEX_SESSION_ID": "x1"}))
        self.assertIsNone(rs.session_id_from_env("codex", {"CLAUDE_CODE_SESSION_ID": "c1"}))
        self.assertIsNone(rs.session_id_from_env("claude", {}))
        self.assertIsNone(rs.session_id_from_env("codex", {}))
        self.assertEqual(rs.session_id_from_env("claude", {"CLAUDE_CODE_SESSION_ID": "a" * 128}), "a" * 128)
        for value in UNSAFE_IDS:
            for host, name in (("claude", "CLAUDE_CODE_SESSION_ID"), ("codex", "CODEX_SESSION_ID")):
                self.assertIsNone(rs.session_id_from_env(host, {name: value}), (host, value))
        with self.assertRaises(ValueError):
            rs.session_id_from_env("gemini", {"CLAUDE_CODE_SESSION_ID": "a"})

    def test_no_id_creates_no_file(self):
        os.makedirs(self.base)
        before = snapshot(self.base)
        for host in ("claude", "codex"):
            self.assertIsNone(rs.session_id_from_env(host, {}))
            self.assertIsNone(rs.session_id_from_env(host, {"OTHER": "x"}))
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(os.listdir(self.root), ["sessions"])

    # D4
    def test_set_mode_on_no_file_for_each_mode_writes_the_whole_state(self):
        for host in ("claude", "codex"):
            for mode in ("off", "gate", "each", None):
                with self.subTest(host=host, mode=mode):
                    shutil.rmtree(self.base, ignore_errors=True)
                    self.assertIsNone(self.set_mode(mode, "m1", AT, host))
                    self.assertEqual(self.read(host), ("valid", state_of(
                        host, SID, mode, {"id": "m1", "at": AT}, processed=["m1"])))
                    self.assertEqual(json.loads(self.raw(host)), state_of(
                        host, SID, mode, {"id": "m1", "at": AT}, processed=["m1"]))
                    self.assertEqual(sorted(os.listdir(self.base)), [host + "-sess-1.json", host + "-sess-1.lock"])

    def test_set_mode_on_an_existing_state_keeps_the_authorizations_and_processed_events(self):
        self.authorize("e1", AT, tier="step")
        self.authorize("e2", AT2, tier="gate")
        authorizations = [authorization("e1"), authorization("e2", AT2, tier="gate")]
        sequence = [("off", "m1", AT), ("gate", "m2", AT2), ("each", "m3", AT), (None, "m4", AT2), ("off", "m5", AT)]
        stored = ["e1", "e2"]
        for mode, event, at in sequence:
            self.set_mode(mode, event, at)
            stored.append(event)
            self.assertEqual(self.read(), ("valid", state_of(
                mode=mode, mode_event={"id": event, "at": at}, authorizations=authorizations,
                processed=stored)), (mode, event))

    def test_set_mode_null_keeps_mode_event_as_provenance(self):
        self.set_mode("each", "m1", AT)
        self.set_mode(None, "m2", AT2)
        self.assertEqual(self.read(), ("valid", state_of(
            mode=None, mode_event={"id": "m2", "at": AT2}, processed=["m1", "m2"])))

    def test_set_mode_with_the_same_event_id_changes_nothing(self):
        self.set_mode("off", "m1", AT)
        self.authorize("e1")
        before = snapshot(self.base)
        for mode, at in (("off", AT), ("gate", AT2), (None, AT2), ("each", AT)):
            self.assertIsNone(self.set_mode(mode, "m1", at))
            self.assertEqual(snapshot(self.base), before, (mode, at))
        self.assertEqual(self.read()[1]["mode"], "off")
        self.assertEqual(self.read()[1]["mode_event"], {"id": "m1", "at": AT})
        # a new event id with the same mode is a change
        self.set_mode("off", "m2", AT2)
        self.assertEqual(self.read()[1]["mode_event"], {"id": "m2", "at": AT2})
        self.assertNotEqual(snapshot(self.base)["claude-sess-1.json"], before["claude-sess-1.json"])
        # an old event id that is not the current one changes nothing either (D2)
        after = snapshot(self.base)
        self.set_mode("gate", "m1", AT)
        self.assertEqual(snapshot(self.base), after)
        self.assertEqual(self.read()[1]["mode"], "off")
        self.assertEqual(self.read()[1]["mode_event"], {"id": "m2", "at": AT2})
        self.assertEqual(self.read()[1]["processed_events"], ["m1", "e1", "m2"])

    def test_set_mode_event_id_equal_to_an_authorization_event_id_changes_nothing(self):
        self.authorize("same")
        before = snapshot(self.base)
        self.set_mode("each", "same", AT2)
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.read()[1]["mode"], None)
        self.assertEqual(self.read()[1]["mode_event"], None)

    # D7
    def test_clear_removes_the_file_and_is_quiet_without_one(self):
        self.assertIsNone(rs.clear("claude", SID, self.base))
        self.assertFalse(os.path.exists(self.base))  # no folder: nothing is created
        self.set_mode("off", "m1")
        self.set_mode("gate", "m1", AT, "codex")
        self.assertIsNone(rs.clear("claude", SID, self.base))
        self.assertFalse(os.path.exists(self.path()))
        self.assertTrue(os.path.exists(self.path("codex")))
        self.assertEqual(self.read(), ("absent", None))
        lock = os.lstat(self.lock_path())
        before = snapshot(self.base)
        self.assertIsNone(rs.clear("claude", SID, self.base))  # again: quiet, nothing changes
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(os.lstat(self.lock_path()).st_ino, lock.st_ino)  # the lock file stays

    def test_clear_removes_an_invalid_file_and_nothing_else(self):
        self.put(b"junk")
        Path(self.base, "claude-sess-2.json").write_bytes(b"other")
        rs.clear("claude", SID, self.base)
        self.assertEqual(sorted(os.listdir(self.base)), ["claude-sess-1.lock", "claude-sess-2.json"])

    # D8 modes and syncs
    def test_modes_of_files_and_created_folders_under_three_umasks(self):
        for mask in (0o022, 0o077, 0o000):
            with self.subTest(mask=oct(mask)):
                shutil.rmtree(self.root + "/deep", ignore_errors=True)
                base = os.path.join(self.root, "deep", "level", "sessions")
                old = os.umask(mask)
                try:
                    rs.set_mode("claude", SID, "off", "m1", AT, base)
                    first = modes_below(os.path.join(self.root, "deep"))
                    rs.authorize("claude", SID, "e1", AT, "p", "r", "l", "step", "d", base)
                    rs.consume("claude", SID, "p", "r", "l", "step", "d", base)
                    rs.set_mode("claude", SID, "gate", "m2", AT, base)
                finally:
                    os.umask(old)
                expected = {".": 0o700, "level": 0o700, "level/sessions": 0o700,
                            "level/sessions/claude-sess-1.json": 0o600, "level/sessions/claude-sess-1.lock": 0o600}
                self.assertEqual(first, expected)
                self.assertEqual(modes_below(os.path.join(self.root, "deep")), expected)

    def test_existing_folders_keep_their_mode_and_a_replaced_state_gets_0600(self):
        os.makedirs(os.path.join(self.root, "outer"))
        os.chmod(os.path.join(self.root, "outer"), 0o750)
        base = os.path.join(self.root, "outer", "sessions")
        os.makedirs(base)
        os.chmod(base, 0o755)
        old = os.umask(0o022)
        try:
            rs.set_mode("claude", SID, "off", "m1", AT, base)
            Path(base, "claude-sess-1.json").chmod(0o644)
            rs.set_mode("claude", SID, "gate", "m2", AT, base)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(self.root, "outer")).st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(os.lstat(base).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(base, "claude-sess-1.json")).st_mode), 0o600)

    def test_a_missing_level_below_an_existing_folder_is_created_0700_and_its_parent_is_kept(self):
        os.makedirs(os.path.join(self.root, "outer"))
        os.chmod(os.path.join(self.root, "outer"), 0o755)
        base = os.path.join(self.root, "outer", "a", "b", "c")
        old = os.umask(0o000)
        made = []
        real_mkdir = os.mkdir
        try:
            with mock.patch.object(os, "mkdir", lambda path, mode=0o777: (made.append((path, mode)), real_mkdir(path, mode))[1]):
                rs.set_mode("codex", SID, "each", "m1", AT, base)
        finally:
            os.umask(old)
        self.assertEqual(made, [(os.path.join(self.root, "outer", part), 0o700) for part in ("a", "a/b", "a/b/c")])
        for part in ("a", "a/b", "a/b/c"):
            self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(self.root, "outer", part)).st_mode), 0o700, part)
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(self.root, "outer")).st_mode), 0o755)

    def test_a_relative_base_is_made_absolute(self):
        old = os.getcwd()
        os.chdir(self.root)
        try:
            rs.set_mode("claude", SID, "off", "m1", AT, "rel-base")
            self.assertEqual(rs.read("claude", SID, "rel-base"), ("valid", state_of(
                mode="off", mode_event={"id": "m1", "at": AT}, processed=["m1"])))
        finally:
            os.chdir(old)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "rel-base", "claude-sess-1.json")))

    def test_publication_writes_a_complete_temporary_file_renames_it_then_syncs_the_folder(self):
        events = []
        real_rename, real_sync = rs._rename, rs._sync_dir

        def rename(source, destination):
            info = os.lstat(source)
            events.append(("rename", os.path.dirname(source), os.path.basename(source), destination,
                           stat.S_IMODE(info.st_mode), json.loads(Path(source).read_bytes()),
                           self.lock_is_held(self.lock_path())))
            real_rename(source, destination)

        def sync(folder):
            events.append(("sync", folder, os.path.exists(self.path()), sorted(os.listdir(folder)),
                           self.lock_is_held(self.lock_path())))
            real_sync(folder)

        with mock.patch.object(rs, "_rename", rename), mock.patch.object(rs, "_sync_dir", sync):
            self.set_mode("off", "m1")
        expected = state_of(mode="off", mode_event={"id": "m1", "at": AT}, processed=["m1"])
        self.assertEqual(len(events), 2)
        kind, folder, name, destination, mode, content, held = events[0]
        self.assertEqual((kind, folder, destination, mode, content, held),
                         ("rename", self.base, self.path(), 0o600, expected, True))
        self.assertTrue(name.startswith(".tmp-") and len(name) > len(".tmp-"))
        self.assertEqual(events[1], ("sync", self.base, True, ["claude-sess-1.json", "claude-sess-1.lock"], True))

    def test_clear_unlinks_then_syncs_the_folder(self):
        self.set_mode("off", "m1")
        events = []
        real_sync = rs._sync_dir

        def sync(folder):
            events.append((folder, os.path.exists(self.path()), self.lock_is_held(self.lock_path())))
            real_sync(folder)

        with mock.patch.object(rs, "_sync_dir", sync):
            rs.clear("claude", SID, self.base)
            rs.clear("claude", SID, self.base)  # nothing to remove: no sync
        self.assertEqual(events, [(self.base, False, True)])

    def test_no_change_means_no_rename_and_no_sync(self):
        self.set_mode("off", "m1")
        self.authorize("e1")
        calls = []
        with mock.patch.object(rs, "_rename", lambda *a: calls.append(("rename", a))), \
                mock.patch.object(rs, "_sync_dir", lambda *a: calls.append(("sync", a))), \
                mock.patch.object(rs, "_write", lambda *a: calls.append(("write", a))):
            self.set_mode("gate", "m1", AT2)
            self.authorize("e1", AT2)
            self.assertIsNone(self.consume(tier="gate"))
        self.assertEqual(calls, [])

    def test_write_syncs_the_data_and_closes_the_descriptor(self):
        os.makedirs(self.base)
        path = os.path.join(self.base, "f")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        synced = []
        real = os.fsync
        with mock.patch.object(os, "fsync", lambda value: (synced.append(value), real(value))[1]):
            rs._write(fd, b"abc\n")
        self.assertEqual(synced, [fd])
        self.assertEqual(Path(path).read_bytes(), b"abc\n")
        with self.assertRaises(OSError) as caught:
            os.fstat(fd)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_sync_dir_syncs_the_folder_and_closes_its_descriptor(self):
        os.makedirs(self.base)
        opened = []
        real_open, real_sync = os.open, os.fsync

        def spy_open(path, flags, *rest):
            fd = real_open(path, flags, *rest)
            opened.append((path, fd))
            return fd

        synced = []
        with mock.patch.object(os, "open", spy_open), \
                mock.patch.object(os, "fsync", lambda fd: (synced.append(fd), real_sync(fd))[1]):
            rs._sync_dir(self.base)
        self.assertEqual([path for path, _fd in opened], [self.base])
        self.assertEqual(synced, [opened[0][1]])
        with self.assertRaises(OSError):
            os.fstat(opened[0][1])

    def test_the_file_text_is_the_sorted_json_of_the_state_with_a_final_newline(self):
        self.authorize("e1")
        raw = self.raw()
        self.assertTrue(raw.endswith(b"}\n"))
        self.assertEqual(raw, (json.dumps(self.read()[1], sort_keys=True, indent="  ") + "\n").encode("ascii"))

    # arguments that the module refuses before it touches a file
    def test_wrong_arguments_raise_value_error_and_touch_no_file(self):
        for host in ("gemini", "", None, "Claude"):
            for call in (lambda: rs.read(host, SID, self.base), lambda: self.set_mode("off", "m", AT, host),
                         lambda: self.authorize("e", AT, host), lambda: self.consume(host),
                         lambda: rs.clear(host, SID, self.base)):
                with self.assertRaises(ValueError):
                    call()
        for mode in ("bad", "OFF", 0, False, ["off"], "", True):
            with self.assertRaises(ValueError, msg=repr(mode)):
                self.set_mode(mode, "m1")
        for event in ("", 5, None, ["e"]):
            with self.assertRaises(ValueError, msg=repr(event)):
                self.set_mode("off", event)
            with self.assertRaises(ValueError, msg=repr(event)):
                self.authorize(event)
        for at in BAD_TIMES:
            with self.assertRaises(ValueError, msg=repr(at)):
                self.set_mode("off", "m1", at)
            with self.assertRaises(ValueError, msg=repr(at)):
                self.authorize("e1", at)
        for tier in ("bad", "Step", "", 5, None):
            with self.assertRaises(ValueError, msg=repr(tier)):
                self.authorize("e1", tier=tier)
        for key in ("project_home", "repository", "lineage", "settings_digest"):
            for value in (5, None, ["x"], b"x"):
                with self.assertRaises(ValueError, msg=(key, value)):
                    self.authorize("e1", **{key: value})
        self.assertFalse(os.path.exists(self.base))
        for value in GOOD_TIMES:
            self.set_mode("off", "m-" + value, value)
            self.authorize("e-" + value, value)
        self.authorize("empty-fields", project_home="", repository="", lineage="", settings_digest="")
        self.assertEqual(self.read()[1]["authorizations"][-1], authorization(
            "empty-fields", project_home="", repository="", lineage="", settings_digest=""))


# ----------------------------------------------------------------- ACC-2

class AuthorizationTests(SelfChecks, WithTemp):

    def test_authorize_stores_the_seven_fields_and_the_event_id(self):
        self.assertIsNone(self.authorize("e1", AT))
        expected = authorization("e1", AT)
        self.assertEqual(sorted(expected), sorted(SEVEN))
        self.assertEqual(self.read(), ("valid", state_of(authorizations=[expected], processed=["e1"])))
        self.authorize("e2", AT2, project_home="/other", repository="/r2", lineage="l2", tier="gate",
                       settings_digest="d2")
        second = {"event_id": "e2", "at": AT2, "project_home": "/other", "repository": "/r2", "lineage": "l2",
                  "tier": "gate", "settings_digest": "d2"}
        self.assertEqual(self.read(), ("valid", state_of(authorizations=[expected, second], processed=["e1", "e2"])))

    def test_authorize_on_an_existing_state_keeps_the_mode(self):
        self.set_mode("each", "m1")
        self.authorize("e1")
        self.assertEqual(self.read(), ("valid", state_of(
            mode="each", mode_event={"id": "m1", "at": AT}, authorizations=[authorization("e1")],
            processed=["m1", "e1"])))

    def test_a_second_authorize_with_the_same_event_id_adds_nothing(self):
        self.authorize("e1")
        before = snapshot(self.base)
        self.assertIsNone(self.authorize("e1"))
        self.assertEqual(snapshot(self.base), before)
        self.assertIsNone(self.authorize("e1", AT2, **DIFFERENT))
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.read(), ("valid", state_of(authorizations=[authorization("e1")], processed=["e1"])))
        self.authorize("e2")
        self.assertEqual(self.read()[1]["processed_events"], ["e1", "e2"])

    def test_consume_returns_and_removes_the_earliest_appended_whatever_the_at_values_say(self):
        self.authorize("e1", "2026-11-17T12:00:00Z")
        self.authorize("e2", "2026-11-17T09:00:00Z")
        self.authorize("e3", "2026-11-17T10:00:00Z")
        self.assertEqual(self.consume(), authorization("e1", "2026-11-17T12:00:00Z"))
        self.assertEqual(self.read()[1]["authorizations"], [authorization("e2", "2026-11-17T09:00:00Z"),
                                                            authorization("e3", "2026-11-17T10:00:00Z")])
        self.assertEqual(self.consume(), authorization("e2", "2026-11-17T09:00:00Z"))
        self.assertEqual(self.consume(), authorization("e3", "2026-11-17T10:00:00Z"))
        self.assertIsNone(self.consume())
        self.assertEqual(self.read(), ("valid", state_of(processed=["e1", "e2", "e3"])))

    def test_consume_keeps_the_others_and_skips_the_ones_that_do_not_match(self):
        self.authorize("n1", AT, repository="/other")
        self.authorize("m1", AT)
        self.authorize("n2", AT, tier="gate")
        self.authorize("m2", AT2)
        self.authorize("n3", AT, lineage="x")
        self.assertEqual(self.consume(), authorization("m1", AT))
        self.assertEqual([item["event_id"] for item in self.read()[1]["authorizations"]], ["n1", "n2", "m2", "n3"])
        self.assertEqual(self.consume(), authorization("m2", AT2))
        self.assertEqual(self.read()[1]["authorizations"], [authorization("n1", AT, repository="/other"),
                                                            authorization("n2", AT, tier="gate"),
                                                            authorization("n3", AT, lineage="x")])
        self.assertIsNone(self.consume())
        self.assertEqual(self.consume(tier="gate"), authorization("n2", AT, tier="gate"))
        self.assertEqual(self.read()[1]["processed_events"], ["n1", "m1", "n2", "m2", "n3"])

    def test_consume_returns_the_dict_with_exactly_the_seven_fields(self):
        self.authorize("e1")
        result = self.consume()
        self.assertEqual(sorted(result), sorted(SEVEN))
        self.assertEqual(result, authorization("e1"))

    def test_an_authorize_with_the_event_id_of_a_consumed_authorization_adds_nothing(self):
        self.authorize("e1")
        self.assertEqual(self.consume(), authorization("e1"))
        before = snapshot(self.base)
        self.assertIsNone(self.authorize("e1"))
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.read(), ("valid", state_of(processed=["e1"])))
        self.assertIsNone(self.consume())

    def test_a_difference_in_any_one_target_field_gives_null_and_removes_nothing(self):
        self.authorize("e1")
        before = snapshot(self.base)
        for name in TARGET_NAMES:
            self.assertIsNone(self.consume(**{name: DIFFERENT[name]}), name)
            self.assertEqual(snapshot(self.base), before, name)
        self.assertIsNone(self.consume(project_home="/home/p "))
        self.assertIsNone(self.consume(tier="Step"))
        self.assertIsNone(self.consume(tier=None))
        self.assertIsNone(self.consume(lineage=5))
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.consume(), authorization("e1"))

    def test_the_other_fields_do_not_have_to_match(self):
        self.authorize("e1", AT)
        result = rs.consume("claude", SID, TARGET["project_home"], TARGET["repository"], TARGET["lineage"],
                            TARGET["tier"], TARGET["settings_digest"], self.base)
        self.assertEqual(result["event_id"], "e1")

    def test_consume_without_a_file_or_a_folder_gives_null(self):
        self.assertIsNone(self.consume())
        self.assertFalse(os.path.exists(self.base))
        os.makedirs(self.base)
        self.assertIsNone(self.consume())
        self.assertEqual(without_locks(snapshot(self.base)), {})
        self.authorize("e1", host="codex")
        self.assertIsNone(self.consume())  # the state of another host is not used
        self.assertIsNone(self.consume(sid="sess-2"))

    def test_a_mode_change_keeps_the_authorizations(self):
        self.authorize("e1")
        self.authorize("e2", AT2, tier="gate")
        for index, mode in enumerate(("off", "gate", "each", None)):
            self.set_mode(mode, "m%d" % index)
            self.assertEqual(self.read()[1]["authorizations"], [authorization("e1"), authorization("e2", AT2, tier="gate")])
            self.assertEqual(self.read()[1]["processed_events"], ["e1", "e2"] + ["m%d" % n for n in range(index + 1)])
        self.assertEqual(self.consume(), authorization("e1"))

    def test_states_of_two_sessions_and_two_hosts_are_separate(self):
        self.authorize("e1", host="claude", sid="s1")
        self.authorize("e1", host="codex", sid="s1")
        self.authorize("e1", host="claude", sid="s2")
        self.assertEqual(self.consume(host="codex", sid="s1"), authorization("e1"))
        self.assertEqual(self.read("claude", "s1")[1]["authorizations"], [authorization("e1")])
        self.assertEqual(self.read("claude", "s2")[1]["authorizations"], [authorization("e1")])
        self.assertEqual(self.read("codex", "s1")[1]["authorizations"], [])

    def test_on_an_invalid_state_set_mode_and_authorize_raise_and_consume_gives_null_and_bytes_stay(self):
        for label, raw, _reason in INVALID:
            self.put(raw)
            before = snapshot(self.base)
            with self.assertRaises(rs.SessionError, msg=label) as caught:
                self.set_mode("off", "m9")
            self.assertEqual(caught.exception.code, "INVALID_STATE", label)
            self.assertEqual(str(caught.exception), "INVALID_STATE", label)
            self.assert_unchanged(before)
            with self.assertRaises(rs.SessionError, msg=label) as caught:
                self.authorize("e9")
            self.assertEqual(caught.exception.code, "INVALID_STATE", label)
            self.assert_unchanged(before)
            self.assertIsNone(self.consume(), label)
            self.assert_unchanged(before)
            self.assertEqual(self.raw(), raw, label)
            self.assertEqual(sorted(os.listdir(self.base)), ["claude-sess-1.json", "claude-sess-1.lock"], label)

    def test_consume_on_an_invalid_state_that_holds_a_matching_authorization_gives_null(self):
        state = good_state()
        state["extra"] = 1
        self.put(jbytes(state))
        before = snapshot(self.base)
        self.assertIsNone(self.consume())
        self.assert_unchanged(before)
        state = good_state()
        state["schema_version"] = 2
        self.put(jbytes(state))
        self.assertIsNone(self.consume())

    def test_clear_removes_an_invalid_file_under_the_lock(self):
        real_unlink = os.unlink
        held = []

        def unlink(path, *rest):
            if path == self.path():
                held.append(self.lock_is_held(self.lock_path()))
            real_unlink(path, *rest)

        for label, raw, _reason in INVALID:
            self.put(raw)
            with mock.patch.object(os, "unlink", unlink):
                self.assertIsNone(rs.clear("claude", SID, self.base), label)
            self.assertFalse(os.path.exists(self.path()), label)
            self.assertEqual(self.read(), ("absent", None), label)
        self.assertEqual(held, [True] * len(INVALID))  # each file was removed while the lock was held
        self.assertEqual(os.listdir(self.base), ["claude-sess-1.lock"])

    def test_clear_removes_a_valid_state_with_its_processed_event_ids(self):
        self.set_mode("each", "m1")
        self.authorize("e1")
        self.authorize("e2")
        self.assertEqual(self.consume(), authorization("e1"))
        self.assertEqual(self.read()[1]["processed_events"], ["m1", "e1", "e2"])
        rs.clear("claude", SID, self.base)
        self.assertEqual(self.read(), ("absent", None))
        self.assertEqual(os.listdir(self.base), ["claude-sess-1.lock"])
        # the processed ids are gone: the consumed event id is accepted again, and so is the mode event id
        self.authorize("e1")
        self.set_mode("gate", "m1", AT2)
        self.assertEqual(self.read(), ("valid", state_of(
            mode="gate", mode_event={"id": "m1", "at": AT2}, authorizations=[authorization("e1")],
            processed=["e1", "m1"])))


# ----------------------------------------------------------------- CON-20261118-15: replayed events

AT3 = "2026-11-17T10:20:32Z"


class ReplayTests(WithTemp):
    """An event id acts once while the state file lives, as a mode event or as a round event (D1 to D3)."""

    def count_renames(self):
        calls = []
        real = rs._rename

        def spy(source, destination):
            calls.append(destination)
            real(source, destination)

        return calls, mock.patch.object(rs, "_rename", spy)

    def test_a_replayed_mode_event_after_a_later_event_changes_nothing(self):
        self.set_mode("off", "A", AT)
        self.set_mode("each", "B", AT2)
        before = snapshot(self.base)
        calls, patch = self.count_renames()
        with patch:
            self.assertIsNone(self.set_mode("off", "A", AT3))
        self.assertEqual(calls, [])
        self.assertEqual(snapshot(self.base), before)  # bytes, mode, inode and time of every file
        self.assertEqual(self.read(), ("valid", state_of(
            mode="each", mode_event={"id": "B", "at": AT2}, processed=["A", "B"])))

    def test_a_replay_changes_nothing_whatever_the_mode_and_the_time_of_the_call(self):
        self.set_mode("off", "A", AT)
        self.set_mode("each", "B", AT2)
        before = snapshot(self.base)
        for mode in ("off", "gate", "each", None):
            for at in (AT, AT2, AT3):
                for event in ("A", "B"):
                    self.assertIsNone(self.set_mode(mode, event, at))
                    self.assertEqual(snapshot(self.base), before, (mode, at, event))
        self.assertEqual(self.read()[1]["processed_events"], ["A", "B"])

    def test_each_event_id_is_stored_once_and_in_the_order_stored(self):
        for mode, event, at in (("off", "A", AT), ("each", "B", AT2), ("off", "A", AT3), ("gate", "C", AT3),
                                ("each", "B", AT), ("off", "C", AT2), (None, "D", AT3), ("gate", "A", AT)):
            self.set_mode(mode, event, at)
        self.assertEqual(self.read(), ("valid", state_of(
            mode=None, mode_event={"id": "D", "at": AT3}, processed=["A", "B", "C", "D"])))

    def test_one_effective_set_mode_is_one_publication_with_mode_event_and_id_together(self):
        calls, patch = self.count_renames()
        with patch:
            self.set_mode("each", "A", AT)
            self.set_mode("off", "B", AT2)
            self.set_mode("gate", "A", AT3)
        self.assertEqual(calls, [self.path(), self.path()])
        self.assertEqual(self.read()[1]["mode"], "off")

    def test_a_set_mode_with_an_event_id_that_an_authorize_used_changes_nothing(self):
        self.set_mode("off", "M", AT)
        self.authorize("R", AT2)
        before = snapshot(self.base)
        for mode in ("off", "gate", "each", None):
            self.assertIsNone(self.set_mode(mode, "R", AT3))
            self.assertEqual(snapshot(self.base), before, mode)
        self.assertEqual(self.read(), ("valid", state_of(
            mode="off", mode_event={"id": "M", "at": AT}, authorizations=[authorization("R", AT2)],
            processed=["M", "R"])))

    def test_an_authorize_with_an_event_id_that_a_set_mode_used_adds_nothing(self):
        self.set_mode("each", "M", AT)
        before = snapshot(self.base)
        self.assertIsNone(self.authorize("M", AT2))
        self.assertIsNone(self.authorize("M", AT3, **DIFFERENT))
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.read(), ("valid", state_of(
            mode="each", mode_event={"id": "M", "at": AT}, processed=["M"])))
        self.assertIsNone(self.consume())

    def test_an_event_id_used_by_set_mode_then_replaced_as_mode_event_still_blocks_authorize(self):
        self.set_mode("each", "M1", AT)
        self.set_mode("off", "M2", AT2)  # M1 is no longer the mode_event
        before = snapshot(self.base)
        self.authorize("M1", AT3)
        self.assertEqual(snapshot(self.base), before)

    def test_after_clear_the_same_event_ids_act_again(self):
        self.set_mode("off", "A", AT)
        self.authorize("R", AT2)
        self.set_mode("each", "B", AT3)
        self.assertEqual(self.consume(), authorization("R", AT2))
        rs.clear("claude", SID, self.base)
        self.assertEqual(self.read(), ("absent", None))
        self.set_mode("gate", "A", AT3)
        self.assertEqual(self.read(), ("valid", state_of(
            mode="gate", mode_event={"id": "A", "at": AT3}, processed=["A"])))
        self.authorize("R", AT2)
        self.set_mode("off", "B", AT)
        self.assertEqual(self.read(), ("valid", state_of(
            mode="off", mode_event={"id": "B", "at": AT}, authorizations=[authorization("R", AT2)],
            processed=["A", "R", "B"])))

    def test_set_mode_to_null_with_a_new_event_id_stores_it_and_a_replay_of_it_changes_nothing(self):
        self.set_mode("each", "A", AT)
        self.set_mode(None, "N", AT2)
        self.assertEqual(self.read(), ("valid", state_of(
            mode=None, mode_event={"id": "N", "at": AT2}, processed=["A", "N"])))
        before = snapshot(self.base)
        self.set_mode("gate", "N", AT3)
        self.set_mode(None, "A", AT3)
        self.assertEqual(snapshot(self.base), before)
        self.set_mode("gate", "G", AT3)
        self.assertEqual(self.read()[1]["mode"], "gate")

    def test_the_states_of_two_hosts_and_two_sessions_remember_their_own_event_ids(self):
        self.set_mode("off", "A", AT)
        self.set_mode("each", "A", AT2, "codex")
        self.set_mode("gate", "A", AT3, "claude", "sess-2")
        self.assertEqual(self.read("claude")[1]["mode"], "off")
        self.assertEqual(self.read("codex")[1]["mode"], "each")
        self.assertEqual(self.read("claude", "sess-2")[1]["mode"], "gate")
        self.set_mode("each", "A", AT3)  # a replay in the first state only
        self.assertEqual(self.read("claude")[1]["mode"], "off")

    def test_a_state_file_of_the_earlier_shape_reads_as_valid_and_accepts_new_events(self):
        # the earlier code did not put the id of the mode_event into processed_events
        old = state_of(mode="off", mode_event={"id": "old", "at": AT}, authorizations=[authorization("e1")],
                       processed=["e1"])
        self.put(jbytes(old))
        self.assertEqual(self.read(), ("valid", old))
        self.set_mode("each", "new", AT2)
        self.assertEqual(self.read(), ("valid", state_of(
            mode="each", mode_event={"id": "new", "at": AT2}, authorizations=[authorization("e1")],
            processed=["e1", "new"])))
        self.authorize("e2", AT2)
        self.assertEqual(self.read()[1]["processed_events"], ["e1", "new", "e2"])
        self.authorize("e1", AT3)  # a round event that the file holds is still remembered
        self.assertEqual(self.read()[1]["processed_events"], ["e1", "new", "e2"])

    def test_in_a_state_file_of_the_earlier_shape_a_replay_of_its_old_mode_event_is_not_prevented(self):
        old = state_of(mode="off", mode_event={"id": "old", "at": AT}, processed=[])
        self.put(jbytes(old))
        self.assertEqual(self.read(), ("valid", old))
        self.set_mode("gate", "old", AT2)  # the stated limit: the old id is not in the list
        self.assertEqual(self.read(), ("valid", state_of(
            mode="gate", mode_event={"id": "old", "at": AT2}, processed=["old"])))
        before = snapshot(self.base)
        self.set_mode("each", "old", AT3)  # now it is remembered
        self.assertEqual(snapshot(self.base), before)
        # the same file, after the old event was replaced by a new one, is not protected against the old id
        self.put(jbytes(old))
        self.set_mode("each", "new", AT2)
        self.set_mode("off", "old", AT3)
        self.assertEqual(self.read(), ("valid", state_of(
            mode="off", mode_event={"id": "old", "at": AT3}, processed=["new", "old"])))


# ----------------------------------------------------------------- ACC-3

WORKER = r'''
import json, os, select, sys, time
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["scripts"])
import readback_session as rs
delay = spec.get("delay")
if delay:
    real_write = rs._write
    def slow_write(fd, data):
        time.sleep(delay)
        real_write(fd, data)
    rs._write = slow_write
if spec.get("crash") == "rename":
    rs._rename = lambda source, destination: os._exit(7)
if spec.get("crash") == "write":
    def half_write(fd, data):
        os.write(fd, data[:len(data) // 2])
        os._exit(8)
    rs._write = half_write
print("ready", flush=True)
sys.stdin.readline()
try:
    result = getattr(rs, spec["call"])(*spec["args"], **spec["kwargs"])
except rs.SessionError as error:
    print(json.dumps({"error": error.code}), flush=True)
    sys.exit(0)
print(json.dumps({"result": result}), flush=True)
'''

PROCESS_TIMEOUT = 15


class ConcurrencyTests(WithTemp):

    def spawn(self, call, *args, delay=None, crash=None, **kwargs):
        spec = {"scripts": str(SCRIPTS), "call": call, "args": list(args), "kwargs": kwargs, "delay": delay,
                "crash": crash}
        proc = subprocess.Popen([sys.executable, "-B", "-c", WORKER, json.dumps(spec)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._reap, proc)
        ready, _, _ = select.select([proc.stdout], [], [], PROCESS_TIMEOUT)
        self.assertTrue(ready, "a process did not become ready within %d seconds" % PROCESS_TIMEOUT)
        self.assertEqual(proc.stdout.readline(), "ready\n")
        return proc

    @staticmethod
    def _reap(proc):
        if proc.poll() is None:
            proc.kill()
        proc.communicate()

    def go(self, proc):
        proc.stdin.write("go\n")
        proc.stdin.flush()

    def finish(self, proc):
        """(exit code, the decoded last line of the output or None)."""
        try:
            out, err = proc.communicate(timeout=PROCESS_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail("a process did not end within %d seconds" % PROCESS_TIMEOUT)
        lines = out.splitlines()
        return proc.returncode, (json.loads(lines[-1]) if lines else None), err

    @staticmethod
    def fields():
        return tuple(TARGET[name] for name in TARGET_NAMES)

    def only_state_and_lock(self, host="claude", sid=SID):
        mine = [name for name in os.listdir(self.base) if name.startswith(host + "-" + sid + ".") or name.startswith(".tmp-")]
        self.assertEqual(sorted(mine), [host + "-" + sid + ".json", host + "-" + sid + ".lock"])

    def test_only_state_and_lock_rejects_a_leftover_a_missing_file_and_a_third_file(self):
        os.makedirs(self.base)
        Path(self.base, "claude-sess-1.json").write_bytes(b"{}")
        with self.assertRaises(AssertionError):
            self.only_state_and_lock()  # no lock file
        Path(self.base, "claude-sess-1.lock").write_bytes(b"")
        self.only_state_and_lock()
        Path(self.base, ".tmp-x").write_bytes(b"")
        with self.assertRaises(AssertionError):
            self.only_state_and_lock()
        os.unlink(os.path.join(self.base, ".tmp-x"))
        Path(self.base, "claude-sess-1.json.x").write_bytes(b"")
        with self.assertRaises(AssertionError):
            self.only_state_and_lock()
        os.unlink(os.path.join(self.base, "claude-sess-1.json.x"))
        os.unlink(os.path.join(self.base, "claude-sess-1.json"))
        with self.assertRaises(AssertionError):
            self.only_state_and_lock()  # no state file

    def test_eight_processes_consume_one_authorization_exactly_once(self):
        self.authorize("e1")
        procs = [self.spawn("consume", "claude", SID, *self.fields(), base=self.base, delay=0.05) for _ in range(8)]
        for proc in procs:
            self.go(proc)
        results = []
        for proc in procs:
            code, result, err = self.finish(proc)
            self.assertEqual((code, err), (0, ""))
            results.append(result)
        got = [item["result"] for item in results if item["result"] is not None]
        self.assertEqual(got, [authorization("e1")])
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(1 for item in results if item["result"] is None), 7)
        self.assertEqual(self.read(), ("valid", state_of(processed=["e1"])))
        self.only_state_and_lock()

    def test_eight_processes_each_authorize_another_event_id_and_leave_eight(self):
        events = ["e%d" % number for number in range(8)]
        procs = [self.spawn("authorize", "claude", SID, event, AT, *self.fields(), base=self.base, delay=0.05)
                 for event in events]
        for proc in procs:
            self.go(proc)
        for proc in procs:
            code, result, err = self.finish(proc)
            self.assertEqual((code, result, err), (0, {"result": None}, ""))
        state = self.read()
        self.assertEqual(state[0], "valid")
        self.assertEqual(sorted(item["event_id"] for item in state[1]["authorizations"]), events)
        self.assertEqual(sorted(state[1]["processed_events"]), events)
        self.assertEqual(len(state[1]["authorizations"]), 8)
        self.assertEqual(len(state[1]["processed_events"]), 8)
        for item in state[1]["authorizations"]:
            self.assertEqual(item, authorization(item["event_id"]))
        self.only_state_and_lock()

    def test_eight_processes_authorize_the_same_event_id_and_leave_one(self):
        procs = [self.spawn("authorize", "claude", SID, "same", AT, *self.fields(), base=self.base, delay=0.05)
                 for _ in range(8)]
        for proc in procs:
            self.go(proc)
        for proc in procs:
            self.assertEqual(self.finish(proc), (0, {"result": None}, ""))
        self.assertEqual(self.read(), ("valid", state_of(authorizations=[authorization("same")], processed=["same"])))

    def test_eight_processes_deliver_one_mode_event_and_leave_it_once(self):
        procs = [self.spawn("set_mode", "claude", SID, "each", "m1", AT2, base=self.base, delay=0.05)
                 for _ in range(8)]
        for proc in procs:
            self.go(proc)
        for proc in procs:
            self.assertEqual(self.finish(proc), (0, {"result": None}, ""))
        self.assertEqual(self.read(), ("valid", state_of(
            mode="each", mode_event={"id": "m1", "at": AT2}, processed=["m1"])))
        self.only_state_and_lock()

    def test_eight_processes_deliver_eight_mode_events_and_leave_all_eight_remembered(self):
        modes = ["off", "gate", "each", None] * 2
        events = ["m%d" % number for number in range(8)]
        procs = [self.spawn("set_mode", "claude", SID, mode, event, AT2, base=self.base, delay=0.05)
                 for mode, event in zip(modes, events)]
        for proc in procs:
            self.go(proc)
        for proc in procs:
            self.assertEqual(self.finish(proc), (0, {"result": None}, ""))
        kind, state = self.read()
        self.assertEqual(kind, "valid")
        self.assertEqual(sorted(state["processed_events"]), events)
        self.assertEqual(len(state["processed_events"]), 8)
        self.assertIn(state["mode_event"]["id"], events)
        self.assertEqual(state["mode_event"], {"id": state["mode_event"]["id"], "at": AT2})
        self.assertEqual(state["mode"], modes[events.index(state["mode_event"]["id"])])
        self.assertEqual(state, state_of(mode=state["mode"], mode_event=state["mode_event"],
                                         processed=state["processed_events"]))
        self.only_state_and_lock()

    def test_eight_processes_deliver_one_event_id_as_a_mode_event_and_as_a_round_event_and_leave_one(self):
        procs = []
        for number in range(8):
            if number % 2:
                procs.append(self.spawn("set_mode", "claude", SID, "each", "x", AT2, base=self.base, delay=0.05))
            else:
                procs.append(self.spawn("authorize", "claude", SID, "x", AT, *self.fields(), base=self.base,
                                        delay=0.05))
        for proc in procs:
            self.go(proc)
        for proc in procs:
            self.assertEqual(self.finish(proc), (0, {"result": None}, ""))
        kind, state = self.read()
        self.assertEqual(kind, "valid")
        self.assertEqual(state["processed_events"], ["x"])
        self.assertEqual(len(state["authorizations"]) + (state["mode_event"] is not None), 1)

    def test_set_mode_and_authorize_at_the_same_time_both_show(self):
        for round_number, order in enumerate(("mode first", "authorize first", "mode first")):
            sid = "round-%d" % round_number
            if round_number == 2:
                self.authorize("old", sid=sid)  # also on an existing state
            mode = self.spawn("set_mode", "claude", sid, "each", "m1", AT2, base=self.base, delay=0.05)
            auth = self.spawn("authorize", "claude", sid, "e1", AT, *self.fields(), base=self.base, delay=0.05)
            for proc in ((mode, auth) if order == "mode first" else (auth, mode)):
                self.go(proc)
            self.assertEqual(self.finish(mode), (0, {"result": None}, ""))
            self.assertEqual(self.finish(auth), (0, {"result": None}, ""))
            old = [authorization("old")] if round_number == 2 else []
            processed = ["old", "e1", "m1"] if round_number == 2 else ["e1", "m1"]
            kind, got = self.read("claude", sid)
            self.assertEqual(kind, "valid")
            self.assertEqual(sorted(got["processed_events"]), sorted(processed))  # the order is a race
            got["processed_events"] = processed
            self.assertEqual(got, state_of(
                sid=sid, mode="each", mode_event={"id": "m1", "at": AT2}, authorizations=old + [authorization("e1")],
                processed=processed))
            self.only_state_and_lock("claude", sid)
            rs.clear("claude", sid, self.base)

    def test_clear_beside_authorize_leaves_either_no_file_or_a_valid_state(self):
        seen = set()
        for round_number in range(6):
            sid = "clear-%d" % round_number
            self.authorize("old", sid=sid)
            auth = self.spawn("authorize", "claude", sid, "new", AT2, *self.fields(), base=self.base,
                              delay=0.05)
            clear = self.spawn("clear", "claude", sid, base=self.base, delay=0.05)
            for proc in ((auth, clear) if round_number % 2 == 0 else (clear, auth)):
                self.go(proc)
            self.assertEqual(self.finish(auth), (0, {"result": None}, ""))
            self.assertEqual(self.finish(clear), (0, {"result": None}, ""))
            outcome = self.read("claude", sid)
            if outcome[0] == "absent":
                self.assertFalse(os.path.exists(self.path("claude", sid)))
                seen.add("absent")
            else:
                self.assertEqual(outcome, ("valid", state_of(
                    sid=sid, authorizations=[authorization("new", AT2)], processed=["new"])))
                seen.add("valid")
            self.assertFalse([name for name in os.listdir(self.base) if name.startswith(".tmp-")])
            self.assertTrue(os.path.exists(self.lock_path("claude", sid)))
        self.assertTrue(seen)

    def test_the_lock_file_stays_and_keeps_its_identity(self):
        self.set_mode("off", "m1")
        first = os.lstat(self.lock_path())
        self.authorize("e1")
        self.consume()
        rs.clear("claude", SID, self.base)
        self.set_mode("off", "m2")
        rs.clear("claude", SID, self.base)
        again = os.lstat(self.lock_path())
        self.assertEqual((again.st_ino, again.st_size), (first.st_ino, 0))
        self.assertEqual(os.listdir(self.base), ["claude-sess-1.lock"])

    def test_every_mutation_waits_for_the_lock_file_of_its_session_and_other_sessions_do_not(self):
        self.authorize("e1")
        self.authorize("e1", sid="other")
        lock_fd = os.open(self.lock_path(), os.O_RDWR)
        self.addCleanup(lambda: os.close(lock_fd) if lock_fd is not None else None)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        before = snapshot(self.base)
        calls = [
            ("set_mode", ("claude", SID, "each", "m9", AT2), {}),
            ("authorize", ("claude", SID, "e9", AT2, *self.fields()), {}),
            ("consume", ("claude", SID, *self.fields()), {}),
            ("clear", ("claude", SID), {}),
        ]
        waiting = []
        for call, args, kwargs in calls:
            waiting.append(self.spawn(call, *args, base=self.base, **kwargs))
        unrelated = self.spawn("set_mode", "claude", "other", "each", "m9", AT2, base=self.base)
        for proc in waiting + [unrelated]:
            self.go(proc)
        self.assertEqual(self.finish(unrelated), (0, {"result": None}, ""))  # another session is not blocked
        self.assertEqual(self.read("claude", "other")[1]["mode"], "each")
        time.sleep(0.5)
        for (call, _args, _kwargs), proc in zip(calls, waiting):
            self.assertIsNone(proc.poll(), call + " did not wait for the lock")
        self.assertEqual(snapshot(self.base)["claude-sess-1.json"], before["claude-sess-1.json"])
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        lock_fd = None
        results = {}
        for (call, _args, _kwargs), proc in zip(calls, waiting):
            code, result, err = self.finish(proc)
            self.assertEqual((code, err), (0, ""), call)
            results[call] = result
        self.assertEqual(results["set_mode"], {"result": None})
        self.assertEqual(results["authorize"], {"result": None})
        self.assertEqual(results["clear"], {"result": None})
        # the order of the four is free: consume before clear takes e1; after clear it finds nothing, or e9 when
        # authorize ran between clear and consume
        self.assertIn(results["consume"]["result"], (None, authorization("e1"), authorization("e9", AT2)))
        # the four ran one after another, never together: the end state is one of the clear outcomes
        self.assertIn(self.read()[0], ("absent", "valid"))

    def test_an_injected_exception_before_the_rename_leaves_the_earlier_state_and_no_temporary_file(self):
        self.set_mode("off", "m1")
        self.authorize("e1")
        earlier = snapshot(self.base)
        for name in ("_write", "_rename"):
            for call in (lambda: self.set_mode("each", "m2", AT2), lambda: self.authorize("e2", AT2),
                         lambda: self.consume()):
                injected = OSError(errno.EIO, "injected " + name)
                with mock.patch.object(rs, name, side_effect=injected):
                    with self.assertRaises(OSError) as caught:
                        call()
                self.assertIs(caught.exception, injected)
                self.assert_unchanged(earlier)
                self.assertEqual(self.read(), ("valid", state_of(
                    mode="off", mode_event={"id": "m1", "at": AT}, authorizations=[authorization("e1")],
                    processed=["m1", "e1"])))
                self.only_state_and_lock()
        # the failed consume did not lose the authorization, and the next call works
        self.assertEqual(self.consume(), authorization("e1"))

    def test_an_injected_exception_on_the_first_write_leaves_no_state_and_no_temporary_file(self):
        with mock.patch.object(rs, "_rename", side_effect=OSError(errno.EIO, "injected")):
            with self.assertRaises(OSError):
                self.set_mode("off", "m1")
        self.assertEqual(self.read(), ("absent", None))
        self.assertEqual(os.listdir(self.base), ["claude-sess-1.lock"])

    def test_a_crash_between_the_temporary_write_and_the_rename_leaves_the_earlier_state(self):
        self.set_mode("off", "m1")
        self.authorize("e1")
        earlier = snapshot(self.base)
        expected = state_of(mode="off", mode_event={"id": "m1", "at": AT}, authorizations=[authorization("e1")],
                            processed=["m1", "e1"])
        crashes = (("rename", 7), ("write", 8))
        for crash, code in crashes:
            proc = self.spawn("set_mode", "claude", SID, "each", "m2", AT2, base=self.base, crash=crash)
            self.go(proc)
            got_code, _result, _err = self.finish(proc)
            self.assertEqual(got_code, code)
            self.assertEqual(without_locks({k: v for k, v in snapshot(self.base).items() if not k.startswith(".tmp-")}),
                             without_locks(earlier))
            self.assertEqual(self.read(), ("valid", expected))
        leftovers = [name for name in os.listdir(self.base) if name.startswith(".tmp-")]
        self.assertEqual(len(leftovers), 2)  # the crash left its temporary files: they exist, read does not use them
        sizes = sorted(os.lstat(os.path.join(self.base, name)).st_size for name in leftovers)
        self.assertGreater(sizes[0], 0)
        self.assertLess(sizes[0], sizes[1])  # the file of the crash during the write is half a state
        # a later mutation works around them and does not take them for state
        self.set_mode("gate", "m3", AT2)
        self.assertEqual(self.read(), ("valid", state_of(
            mode="gate", mode_event={"id": "m3", "at": AT2}, authorizations=[authorization("e1")],
            processed=["m1", "e1", "m3"])))
        self.assertEqual(len([n for n in os.listdir(self.base) if n.startswith(".tmp-")]), 2)

    def test_a_crash_before_any_state_exists_leaves_no_state_and_a_temporary_file(self):
        proc = self.spawn("authorize", "claude", SID, "e1", AT, *self.fields(), base=self.base, crash="rename")
        self.go(proc)
        self.assertEqual(self.finish(proc)[0], 7)
        self.assertEqual(self.read(), ("absent", None))
        leftovers = [name for name in os.listdir(self.base) if name.startswith(".tmp-")]
        self.assertEqual(len(leftovers), 1)
        self.assertEqual(json.loads(Path(self.base, leftovers[0]).read_bytes())["authorizations"], [authorization("e1")])
        self.assertEqual(self.consume(), None)
        # a temporary file that looks like a complete state is still not state
        self.assertEqual(self.read(), ("absent", None))

    def test_a_process_that_dies_while_it_holds_the_lock_releases_it(self):
        self.authorize("e1")
        proc = self.spawn("set_mode", "claude", SID, "each", "m2", AT2, base=self.base, crash="rename")
        self.go(proc)
        self.assertEqual(self.finish(proc)[0], 7)
        self.assertEqual(self.consume(), authorization("e1"))  # not blocked by the dead process


if __name__ == "__main__":
    unittest.main(failfast=True)
