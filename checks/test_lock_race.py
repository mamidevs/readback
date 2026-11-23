#!/usr/bin/env python3
"""Lock-file creation race checks without a second process.

Run: python3 -m unittest discover -s checks -p 'test_lock_race.py' -v
The open call of the lock file is replaced as project_registry sees it.
"""

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import project_registry as pr


class LockRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="readback-lock-race-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.path = self.base / "projects.lock"
        self.real_open = os.open
        self.lock_calls = 0

    def is_lock_open(self, name, flags):
        return name == self.path.name and flags & os.O_CREAT

    def fake_open(self, failures, error=FileNotFoundError):
        """Fail the lock open `failures` times (None means always), then delegate."""
        def opener(name, flags, *args, **kwargs):
            if self.is_lock_open(name, flags):
                self.lock_calls += 1
                if failures is None or self.lock_calls <= failures:
                    raise error(2 if error is FileNotFoundError else 13, "forced")
            return self.real_open(name, flags, *args, **kwargs)
        return opener

    def test_one_missing_file_error_is_retried_and_lock_is_taken(self):
        with mock.patch.object(pr.os, "open", self.fake_open(1)):
            with pr._lock(self.path, time.monotonic() + 5) as parent_fd:
                self.assertIsInstance(parent_fd, int)
        self.assertEqual(self.lock_calls, 2)
        self.assertTrue(self.path.is_file())

    def test_missing_file_error_until_deadline_gives_the_invalid_error(self):
        started = time.monotonic()
        with mock.patch.object(pr.os, "open", self.fake_open(None)):
            with self.assertRaises(pr.RegistryError) as caught:
                with pr._lock(self.path, time.monotonic() + 0.2):
                    self.fail("lock must not be taken")
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "INVALID")
        self.assertEqual(str(caught.exception), "Unsafe or unavailable control directory")
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 1.0)
        self.assertGreater(self.lock_calls, 2)
        # At most 0.02 s between tries bounds the count from below.
        self.assertGreaterEqual(self.lock_calls, 5)

    def test_expired_deadline_still_tries_once_and_stops(self):
        with mock.patch.object(pr.os, "open", self.fake_open(None)):
            with self.assertRaises(pr.RegistryError) as caught:
                with pr._lock(self.path, time.monotonic() - 1):
                    self.fail("lock must not be taken")
        self.assertEqual(caught.exception.code, "INVALID")
        self.assertEqual(self.lock_calls, 1)

    def test_other_os_error_is_not_retried(self):
        with mock.patch.object(pr.os, "open", self.fake_open(None, PermissionError)):
            with self.assertRaises(pr.RegistryError) as caught:
                with pr._lock(self.path, time.monotonic() + 5):
                    self.fail("lock must not be taken")
        self.assertEqual(caught.exception.code, "INVALID")
        self.assertEqual(self.lock_calls, 1)

    def test_symbolic_link_at_the_lock_name_is_refused(self):
        target = self.base / "target"
        target.write_text("x")
        self.path.symlink_to(target)
        with mock.patch.object(pr.os, "open", self.fake_open(0)):
            with self.assertRaises(pr.RegistryError) as caught:
                with pr._lock(self.path, time.monotonic() + 5):
                    self.fail("lock must not be taken")
        self.assertEqual(caught.exception.code, "INVALID")
        # The path check refuses the link before any open call.
        self.assertEqual(self.lock_calls, 0)
        self.assertEqual(target.read_text(), "x")

    def test_no_follow_flag_refuses_a_link_when_the_path_check_is_bypassed(self):
        target = self.base / "target"
        target.write_text("x")
        self.path.symlink_to(target)
        with mock.patch.object(pr, "_secure_components", lambda path, **kw: path), \
                mock.patch.object(pr.os, "open", self.fake_open(0)):
            with self.assertRaises(pr.RegistryError) as caught:
                with pr._lock(self.path, time.monotonic() + 5):
                    self.fail("lock must not be taken")
        self.assertEqual(caught.exception.code, "INVALID")
        self.assertEqual(self.lock_calls, 1)
        self.assertEqual(target.read_text(), "x")

    def test_open_flags_keep_no_follow(self):
        seen = []
        real = self.real_open

        def spy(name, flags, *args, **kwargs):
            if self.is_lock_open(name, flags):
                seen.append(flags)
            return real(name, flags, *args, **kwargs)
        with mock.patch.object(pr.os, "open", spy):
            with pr._lock(self.path, time.monotonic() + 5):
                pass
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0] & os.O_NOFOLLOW)
        self.assertTrue(seen[0] & os.O_CREAT)

    def test_lock_file_with_two_links_is_refused(self):
        self.path.write_text("")
        os.link(self.path, self.base / "second-name")
        with self.assertRaises(pr.RegistryError):
            with pr._lock(self.path, time.monotonic() + 5):
                self.fail("lock must not be taken")

    def record_sleeps(self):
        sleeps = []
        real_sleep = time.sleep

        def recorder(seconds):
            sleeps.append(seconds)
            real_sleep(seconds)
        return sleeps, recorder

    def test_every_pause_between_tries_is_at_most_twenty_milliseconds(self):
        sleeps, recorder = self.record_sleeps()
        with mock.patch.object(pr.os, "open", self.fake_open(None)), \
                mock.patch.object(pr.time, "sleep", recorder):
            with self.assertRaises(pr.RegistryError):
                with pr._lock(self.path, time.monotonic() + 0.2):
                    self.fail("lock must not be taken")
        self.assertGreaterEqual(len(sleeps), 1)
        self.assertEqual(len(sleeps), self.lock_calls - 1)
        for pause in sleeps:
            self.assertLessEqual(pause, 0.02)

    def test_pause_is_shorter_when_less_than_twenty_milliseconds_remain(self):
        sleeps, recorder = self.record_sleeps()
        with mock.patch.object(pr.os, "open", self.fake_open(None)), \
                mock.patch.object(pr.time, "sleep", recorder):
            with self.assertRaises(pr.RegistryError):
                with pr._lock(self.path, time.monotonic() + 0.005):
                    self.fail("lock must not be taken")
        self.assertGreaterEqual(len(sleeps), 1)
        self.assertLessEqual(sleeps[0], 0.005)
        for pause in sleeps:
            self.assertLessEqual(pause, 0.02)

    def test_fifo_at_the_lock_name_is_not_a_regular_file_and_is_refused(self):
        # O_RDWR on a FIFO does not block, so the open call reaches the file kind check.
        os.mkfifo(self.path)
        with self.assertRaises(pr.RegistryError) as caught:
            with pr._lock(self.path, time.monotonic() + 5):
                self.fail("lock must not be taken")
        # The cleanup unlock of a FIFO raises OSError and hides the message of the
        # refusal behind the control-directory error of today. The refusal itself
        # stays visible as the context of that error.
        self.assertEqual(caught.exception.code, "INVALID")
        self.assertEqual(str(caught.exception), "Unsafe or unavailable control directory")
        refusal = caught.exception.__cause__.__context__
        self.assertIsInstance(refusal, pr.RegistryError)
        self.assertEqual(str(refusal), "Lock must be an independent regular file")

    def test_lock_file_replaced_after_the_lock_is_taken_is_detected(self):
        path = self.path
        real_flock = pr.fcntl.flock

        def flock(fd, operation):
            real_flock(fd, operation)
            if operation & pr.fcntl.LOCK_EX:
                # The file passed the kind check; now another file takes the name.
                path.unlink()
                path.write_text("")
        with mock.patch.object(pr.fcntl, "flock", flock):
            with self.assertRaises(pr.RegistryError) as caught:
                with pr._lock(path, time.monotonic() + 5):
                    self.fail("lock must not be taken")
        self.assertEqual(caught.exception.code, "INVALID")
        self.assertEqual(str(caught.exception), "Persistent lock file was replaced")

    def test_parent_directory_replaced_during_the_lock_is_detected(self):
        control = self.base / "control"
        control.mkdir(mode=0o700)
        path = control / "projects.lock"
        real = self.real_open

        def opener(name, flags, *args, **kwargs):
            fd = real(name, flags, *args, **kwargs)
            if self.is_lock_open(name, flags):
                os.rename(control, self.base / "moved")
                control.mkdir(mode=0o700)
            return fd
        with mock.patch.object(pr.os, "open", opener):
            with self.assertRaises(pr.RegistryError) as caught:
                with pr._lock(path, time.monotonic() + 5):
                    self.fail("lock must not be taken")
        self.assertEqual(caught.exception.code, "INVALID")
        self.assertEqual(str(caught.exception), "Control directory was replaced")


if __name__ == "__main__":
    unittest.main()
