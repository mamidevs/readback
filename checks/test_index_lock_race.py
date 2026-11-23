#!/usr/bin/env python3
"""Offline checks for the bounded retry of the index lock open call."""

import fcntl
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import project_index as pi

LOCK = "project-index.lock"
REAL_OPEN = os.open


class FakeClock:
    """Replaces the time module as the script sees it; sleeping advances the clock."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class IndexLockRaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="readback-lock-race-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.fd = REAL_OPEN(self.folder, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.clock = FakeClock()
        patcher = mock.patch.object(pi, "time", types.SimpleNamespace(
            monotonic=self.clock.monotonic, sleep=self.clock.sleep))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def fake_open(self, failures, error=FileNotFoundError):
        """Fail the lock open `failures` times (None means always), then delegate."""
        def opener(path, flags, *args, **kwargs):
            if path != LOCK:
                return REAL_OPEN(path, flags, *args, **kwargs)
            self.calls.append((path, flags, args, kwargs))
            if failures is None or len(self.calls) <= failures:
                raise error(2 if error is FileNotFoundError else 13, "forced")
            return REAL_OPEN(path, flags, *args, **kwargs)
        return mock.patch.object(pi.os, "open", side_effect=opener)

    def test_one_file_not_found_then_success(self):
        with self.fake_open(1):
            with pi.publication_lock(self.fd):
                pass
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.clock.sleeps)
        self.assertTrue(all(0 <= pause <= 0.02 for pause in self.clock.sleeps))
        self.assertTrue((self.folder / LOCK).exists())

    def test_open_flags_are_unchanged_on_every_try(self):
        with self.fake_open(2):
            with pi.publication_lock(self.fd):
                pass
        expected = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        for path, flags, args, kwargs in self.calls:
            self.assertEqual((flags, args, kwargs), (expected, (0o600,), {"dir_fd": self.fd}))

    def test_file_not_found_until_deadline_leaves_the_error_in_bounded_time(self):
        with self.fake_open(None):
            with self.assertRaises(FileNotFoundError):
                with pi.publication_lock(self.fd):
                    self.fail("lock must not be acquired")
        elapsed = self.clock.now - 1000.0
        self.assertGreaterEqual(elapsed, 5)
        self.assertLess(elapsed, 5.05)
        self.assertGreater(len(self.calls), 2)
        self.assertTrue(all(0 <= pause <= 0.02 for pause in self.clock.sleeps))

    def test_other_oserror_is_not_retried(self):
        with self.fake_open(None, PermissionError):
            with self.assertRaises(PermissionError):
                with pi.publication_lock(self.fd):
                    self.fail("lock must not be acquired")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_lock_held_past_the_deadline_gives_busy(self):
        holder = REAL_OPEN(LOCK, os.O_RDWR | os.O_CREAT, 0o600, dir_fd=self.fd)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.fake_open(0):
            with self.assertRaises(pi.IndexError) as caught:
                with pi.publication_lock(self.fd):
                    self.fail("lock must not be acquired")
        self.assertEqual((str(caught.exception), caught.exception.status), ("Another index builder is active", "BUSY"))
        self.assertEqual(len(self.calls), 1)
        self.assertGreaterEqual(self.clock.now - 1000.0, 5)

    def test_one_deadline_bounds_the_open_retries_and_the_lock_wait(self):
        holder = REAL_OPEN(LOCK, os.O_RDWR | os.O_CREAT, 0o600, dir_fd=self.fd)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.fake_open(100):  # 100 tries * 0.02 s = 2 s spent before the open succeeds
            with self.assertRaises(pi.IndexError) as caught:
                with pi.publication_lock(self.fd):
                    self.fail("lock must not be acquired")
        self.assertEqual(caught.exception.status, "BUSY")
        self.assertLess(self.clock.now - 1000.0, 5.1)


if __name__ == "__main__":
    unittest.main()
