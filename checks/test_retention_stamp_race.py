#!/usr/bin/env python3
"""Offline checks for the single retry of the daily stamp open call."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import readback_settings
import review_retention as retention

REAL_OPEN = os.open


class StampRaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        env = patch.dict(os.environ, HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.project = readback_settings.resolve(str(self.repo), "codex")
        # A store makes the clock folder the shared ledger folder and skips the first scan.
        (Path(self.project["git_common_dir"]) / "readback").mkdir(parents=True, exist_ok=True)
        self.calls = []

    def fake_open(self, failures, error=FileNotFoundError):
        def opener(path, flags, *args, **kwargs):
            if not str(path).startswith(".retention-"):
                return REAL_OPEN(path, flags, *args, **kwargs)
            self.calls.append((path, flags, args, kwargs))
            if len(self.calls) <= failures:
                raise error(2 if error is FileNotFoundError else 13, "forced")
            return REAL_OPEN(path, flags, *args, **kwargs)
        return patch.object(retention.os, "open", side_effect=opener)

    def stamps(self):
        return list((self.home / "logbook" / ".readback").glob(".retention-*"))

    def test_one_file_not_found_then_success_with_two_open_calls(self):
        with self.fake_open(1):
            retention._run(self.project, apply=True, daily=True)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.stamps()), 1)
        self.assertTrue(self.stamps()[0].read_bytes().isdigit())

    def test_open_flags_are_unchanged_on_both_tries(self):
        with self.fake_open(1):
            retention._run(self.project, apply=True, daily=True)
        expected = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        for path, flags, args, kwargs in self.calls:
            self.assertEqual(flags, expected)
            self.assertEqual(args, (0o600,))
            self.assertEqual(set(kwargs), {"dir_fd"})

    def test_two_file_not_found_in_a_row_leave_the_error_after_two_calls(self):
        with self.fake_open(2):
            with self.assertRaises(FileNotFoundError):
                retention._run(self.project, apply=True, daily=True)
            self.assertEqual(len(self.calls), 2)
            self.calls.clear()
            retention.maybe_prune(self.project)  # the daily path still swallows it
            self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.stamps(), [])

    def test_other_oserror_is_not_retried(self):
        with self.fake_open(5, PermissionError):
            with self.assertRaises(PermissionError):
                retention._run(self.project, apply=True, daily=True)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
