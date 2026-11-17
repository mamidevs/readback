#!/usr/bin/env python3
"""Offline checks for scripts/review_capture.py, the content capture of a Git worktree.

Run one class: python3 -B checks/test_review_capture.py FormatTests
Run all:       python3 -m unittest discover -s checks -p 'test_review_capture.py' -v

FormatTests, StateTests and CLITests write nothing: no temporary file or folder. They read this repository
and the fixture tree checks/fixtures/review_capture/, and they call Git in read-only mode.
RepoStateTests, IdentityTests, NoWriteTests and RepoCLITests build temporary Git repositories and need a
writable temporary folder.
"""

import contextlib
import signal
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import warnings
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "review_capture.py"
FIXTURE = ROOT / "checks" / "fixtures" / "review_capture"
FIXTURE_PATHS = [b"link", b"plain.txt", b"run.sh", b"sub/inner.txt"]
sys.path.insert(0, str(ROOT / "scripts"))
import review_capture  # noqa: E402

MAGIC = b"readback-manifest-v1\0"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
STAT_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def expected_identity(records):
    """Independent encoder: records are (type, exe, hash, size, path) already in the wanted order."""
    blob = MAGIC
    for kind, exe, digest, size, path in records:
        blob += kind + b" " + exe + b" " + digest + b" " + size + b"\0" + path + b"\0"
    return hashlib.sha256(blob).hexdigest()


def git_out(root, *args):
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], capture_output=True, check=False)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


def own_identity(root):
    """The identity of a worktree from two `git ls-files -z` calls and own reads. No module code is used.

    Only a path from --cached may be absent. A vanished untracked path, a directory or a path that is
    neither a regular file nor a symlink makes the function raise.
    """
    cached = set(p for p in git_out(root, "ls-files", "-z", "--cached").split(b"\0") if p)
    others = set(p for p in git_out(root, "ls-files", "-z", "--others", "--exclude-standard").split(b"\0") if p)
    blob = MAGIC
    for path in sorted(cached | others):
        full = os.path.join(os.fsencode(str(root)), path)
        try:
            info = os.lstat(full)
        except (FileNotFoundError, NotADirectoryError):
            if path not in cached:
                raise AssertionError("an untracked listed path vanished: %r" % path)
            blob += b"a - - -\0" + path + b"\0"
            continue
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(full)
            record = b"l - " + sha(target).encode() + b" " + str(len(target)).encode()
        elif stat.S_ISREG(info.st_mode):
            with open(full, "rb") as handle:
                data = handle.read()
            record = (b"f x " if info.st_mode & 0o100 else b"f - ") + sha(data).encode() + b" " + str(len(data)).encode()
        else:
            raise AssertionError("a listed path is neither a regular file nor a symlink: %r" % path)
        blob += record + b"\0" + path + b"\0"
    return hashlib.sha256(blob).hexdigest(), len(cached | others)


def stat_like(info, **changes):
    fields = {name: getattr(info, name) for name in STAT_FIELDS}
    fields.update(changes)
    return types.SimpleNamespace(**fields)


@contextlib.contextmanager
def patched(**attrs):
    with contextlib.ExitStack() as stack:
        for name, value in attrs.items():
            stack.enter_context(mock.patch.object(review_capture, name, value))
        yield


class FormatTests(unittest.TestCase):
    """In memory only: no temporary file or folder and no Git call."""

    def entries(self):
        return [
            (b"zeta.txt", "f", "-", sha(b"zeta"), 4),
            (b"bin/run.sh", "f", "x", sha(b"#!/bin/sh\n"), 10),
            (b"link", "l", "-", sha(b"bin/run.sh"), 10),
            (b"gone.txt", "a", "-", "-", "-"),
            (b"line\nbreak.txt", "f", "-", EMPTY_SHA, 0),
            (b"caf\xe9-latin1.txt", "f", "-", sha(b"x"), 1),
        ]

    def expected_sorted(self):
        by_hand = [
            (b"bin/run.sh", b"f", b"x", sha(b"#!/bin/sh\n").encode(), b"10"),
            (b"caf\xe9-latin1.txt", b"f", b"-", sha(b"x").encode(), b"1"),
            (b"gone.txt", b"a", b"-", b"-", b"-"),
            (b"line\nbreak.txt", b"f", b"-", EMPTY_SHA.encode(), b"0"),
            (b"link", b"l", b"-", sha(b"bin/run.sh").encode(), b"10"),
            (b"zeta.txt", b"f", b"-", sha(b"zeta").encode(), b"4"),
        ]
        return [(kind, exe, digest, size, path) for path, kind, exe, digest, size in by_hand]

    def test_empty_list_hashes_only_the_magic(self):
        self.assertEqual(review_capture.manifest_digest([]), hashlib.sha256(MAGIC).hexdigest())

    def test_one_record_matches_the_documented_bytes(self):
        got = review_capture.manifest_digest([(b"a.txt", "f", "-", sha(b"hi"), 2)])
        blob = b"readback-manifest-v1\0" + b"f - " + sha(b"hi").encode() + b" 2\0a.txt\0"
        self.assertEqual(got, hashlib.sha256(blob).hexdigest())

    def test_fixed_vector_with_newline_and_non_utf8_paths(self):
        got = review_capture.manifest_digest(self.entries())
        self.assertEqual(got, expected_identity(self.expected_sorted()))

    def test_absent_record_uses_dashes(self):
        got = review_capture.manifest_digest([(b"gone", "a", "-", "-", "-")])
        self.assertEqual(got, hashlib.sha256(MAGIC + b"a - - -\0gone\0").hexdigest())

    def test_order_of_input_does_not_change_the_identity(self):
        entries = self.entries()
        want = expected_identity(self.expected_sorted())
        self.assertEqual(review_capture.manifest_digest(entries), want)
        self.assertEqual(review_capture.manifest_digest(list(reversed(entries))), want)
        self.assertEqual(review_capture.manifest_digest(sorted(entries, key=lambda e: e[3])), want)

    def test_the_module_sorts_by_path_bytes(self):
        entries = self.entries()
        unsorted_blob = MAGIC
        for path, kind, exe, digest, size in entries:
            unsorted_blob += (kind + " " + exe + " " + digest + " " + str(size)).encode() + b"\0" + path + b"\0"
        self.assertNotEqual(hashlib.sha256(unsorted_blob).hexdigest(), review_capture.manifest_digest(entries))
        # Byte order: "B" < "a" < "caf\xe9" < "caf\xc3\xa9" < "\xff". A text sort would place b"caf\xe9" elsewhere.
        paths = [b"\xff", b"a", b"caf\xc3\xa9", b"B", b"caf\xe9"]
        got = review_capture.manifest_digest([(p, "f", "-", EMPTY_SHA, 0) for p in paths])
        order = [b"B", b"a", b"caf\xc3\xa9", b"caf\xe9", b"\xff"]
        self.assertEqual(order, sorted(paths))
        self.assertEqual(got, expected_identity([(b"f", b"-", EMPTY_SHA.encode(), b"0", p) for p in order]))

    def test_each_field_changes_the_identity(self):
        base = (b"p", "f", "-", sha(b"a"), 1)
        ref = review_capture.manifest_digest([base])
        for other in (
            (b"q", "f", "-", sha(b"a"), 1), (b"p", "a", "-", sha(b"a"), 1), (b"p", "f", "x", sha(b"a"), 1),
            (b"p", "f", "-", sha(b"b"), 1), (b"p", "f", "-", sha(b"a"), 2),
        ):
            self.assertNotEqual(review_capture.manifest_digest([other]), ref)

    def test_merge_paths_drops_duplicates_and_sorts_by_bytes(self):
        cached = b"b\0a\0b\0caf\xe9\0"
        others = b"a\0\xff\0B\0"
        self.assertEqual(review_capture.merge_paths(cached, others), [b"B", b"a", b"b", b"caf\xe9", b"\xff"])
        self.assertEqual(review_capture.merge_paths(b"", b""), [])

    def test_capture_of_this_repository_matches_an_independent_computation(self):
        want, count = own_identity(ROOT)
        got = review_capture.capture(str(ROOT), 60)
        self.assertEqual(got["state"], "ok", got)
        self.assertEqual(got["manifest_sha256"], want)
        self.assertEqual(got["entries"], count)
        self.assertEqual(got["root"], os.fsdecode(git_out(ROOT, "rev-parse", "--show-toplevel")).strip())
        self.assertGreater(count, 100)

    def test_fixture_tree_through_the_real_stat_and_read_functions(self):
        deadline = review_capture._clock() + 60
        entries = review_capture._collect(os.fsencode(str(FIXTURE)), FIXTURE_PATHS, set(FIXTURE_PATHS), deadline)
        by_hand = [
            (b"link", "l", "-", "a8404455861b4e579ca623407b673474034a925611df24a572dab4eadc261285", 9),
            (b"plain.txt", "f", "-", "dacf36547c7774a0a170806363b5d412991fbc0d6260b2c00b1d3a80a816c23f", 6),
            (b"run.sh", "f", "x", "6ecf06f6dbbab6a920b5b208bc7c4069ca266b150d6c00533a00b5975a8417ca", 23),
            (b"sub/inner.txt", "f", "-", "33bf6fbd7cd8379785a21e233d8e09f824e7bab459168a96312c1c882c1d7e1f", 5),
        ]
        self.assertEqual(entries, by_hand)
        self.assertEqual((FIXTURE / "plain.txt").read_bytes(), b"plain\n")
        self.assertEqual((FIXTURE / "run.sh").read_bytes(), b"#!/bin/sh\necho fixture\n")
        self.assertEqual((FIXTURE / "sub" / "inner.txt").read_bytes(), b"inner")
        self.assertEqual(os.readlink(FIXTURE / "link"), "plain.txt")
        self.assertTrue(os.lstat(FIXTURE / "run.sh").st_mode & 0o100)
        self.assertFalse(os.lstat(FIXTURE / "plain.txt").st_mode & 0o100)
        self.assertTrue(stat.S_ISLNK(os.lstat(FIXTURE / "link").st_mode))
        records = [(b"l", b"-", by_hand[0][3].encode(), b"9", b"link"), (b"f", b"-", by_hand[1][3].encode(), b"6", b"plain.txt"),
                   (b"f", b"x", by_hand[2][3].encode(), b"23", b"run.sh"), (b"f", b"-", by_hand[3][3].encode(), b"5", b"sub/inner.txt")]
        self.assertEqual(review_capture.manifest_digest(entries), expected_identity(records))

    def test_fixture_symlink_is_hashed_by_its_target_not_followed(self):
        deadline = review_capture._clock() + 60
        entries = review_capture._collect(os.fsencode(str(FIXTURE)), [b"link"], {b"link"}, deadline)
        self.assertEqual(entries, [(b"link", "l", "-", sha(b"plain.txt"), 9)])
        self.assertNotEqual(entries[0][3], sha(b"plain\n"))

    def test_paths_from_cached_and_from_others_both_enter_the_identity(self):
        git = FakeGit(cached=[b"link", b"plain.txt"], others=[b"run.sh", b"sub/inner.txt"])
        with patched(_git=git):
            got = review_capture.capture(str(FIXTURE), 30)
        self.assertEqual(got["state"], "ok", got)
        self.assertEqual(got["entries"], 4)
        with patched(_git=FakeGit()):
            whole = review_capture.capture(str(FIXTURE), 30)
        self.assertEqual(got["manifest_sha256"], whole["manifest_sha256"])
        with patched(_git=FakeGit(cached=[b"link", b"plain.txt"], others=[b"run.sh"])):
            fewer = review_capture.capture(str(FIXTURE), 30)
        self.assertNotEqual(fewer["manifest_sha256"], got["manifest_sha256"])
        self.assertEqual(fewer["entries"], 3)

    def test_only_the_owner_execute_bit_decides_the_executable_flag(self):
        # An injected stat gives plain.txt another permission mode, to the path and to the descriptor alike.
        real_lstat, real_fstat = os.lstat, os.fstat
        plain = str(FIXTURE / "plain.txt")
        cases = {
            0o654: "-",  # group has r-x: not the owner bit
            0o610: "-",  # group execute only
            0o601: "-",  # other execute only
            0o611: "-",  # group and other execute
            0o444: "-",
            0o100: "x",  # owner execute only
            0o745: "x",
            0o755: "x",
            0o700: "x",
        }
        for mode, flag in cases.items():
            with self.subTest(mode=oct(mode)):
                def lstat(path, mode=mode):
                    info = real_lstat(path)
                    return stat_like(info, st_mode=stat.S_IFREG | mode) if os.fsdecode(path) == plain else info

                def fstat(fd, mode=mode):
                    return stat_like(real_fstat(fd), st_mode=stat.S_IFREG | mode)
                with patched(_lstat=lstat, _fstat=fstat):
                    deadline = review_capture._clock() + 60
                    entries = review_capture._collect(os.fsencode(str(FIXTURE)), [b"plain.txt"], {b"plain.txt"}, deadline)
                self.assertEqual(entries[0][1:3], ("f", flag))

    def test_fake_git_rejects_a_wrong_command(self):
        git = FakeGit()
        deadline = review_capture._clock() + 60
        for wrong in (["ls-files", "--cached"], ["ls-files", "-z", "--cached", "--others"], ["ls-files", "-z", "--others"],
                      ["ls-files", "--others", "--exclude-standard", "-z", "x"], ["status"]):
            with self.assertRaises(AssertionError):
                git(str(FIXTURE), wrong, deadline)
        with self.assertRaises(AssertionError):
            git("/", FakeGit.CACHED, deadline)

    def test_own_identity_raises_for_a_vanished_untracked_path_and_for_a_special_path(self):
        listing = {("--cached",): b"gone.txt\0", ("--others", "--exclude-standard"): b"plain.txt\0"}
        def fake(root, *args):
            return listing[tuple(args[2:])]
        with mock.patch.object(sys.modules[__name__], "git_out", fake):
            self.assertEqual(own_identity(FIXTURE)[1], 2)  # the tracked path may be absent
            listing[("--cached",)] = b""
            listing[("--others", "--exclude-standard")] = b"gone.txt\0"
            with self.assertRaises(AssertionError):
                own_identity(FIXTURE)  # an untracked path that vanished
            listing[("--others", "--exclude-standard")] = b"sub\0"
            with self.assertRaises(AssertionError):
                own_identity(FIXTURE)  # a directory is neither a regular file nor a symlink

    def test_the_hash_and_size_do_not_depend_on_the_chunk_size(self):
        # Narrow claim: the hash and the size are the same for several chunk sizes. It says nothing about the
        # number or the place of the deadline checks; StateTests.test_one_chunk_plus_one_byte_has_three_read_loop_checks
        # covers those with the module's own chunk size.
        run_sh = os.fsencode(str(FIXTURE / "run.sh"))
        info = os.lstat(run_sh)
        want = (sha(b"#!/bin/sh\necho fixture\n"), 23)
        for chunk in (1, 2, 3, 7, 22, 23, 24, 4096, 1 << 20, 2 << 20, 1 << 21):
            calls = []
            with self.subTest(chunk=chunk):
                with patched(CHUNK=chunk):
                    got = review_capture._read_file(run_sh, info, lambda: calls.append(1))
                self.assertEqual(got, want)
                self.assertEqual(len(calls), -(-23 // chunk) + 1)  # one check before each read, the last read is empty

    def test_absent_tracked_path_on_the_fixture_is_type_a(self):
        deadline = review_capture._clock() + 60
        entries = review_capture._collect(os.fsencode(str(FIXTURE)), [b"ghost.txt"], {b"ghost.txt"}, deadline)
        self.assertEqual(entries, [(b"ghost.txt", "a", "-", "-", "-")])


REDIRECTING = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
               "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES", "GIT_CONFIG_COUNT",
               "GIT_CONFIG_PARAMETERS", "GIT_CONFIG", "GIT_OPTIONAL_LOCKS")


def clear_git_environment():
    """Remove what could supply or hide a test condition: redirecting variables, trace variables and command-scope config."""
    for name in list(os.environ):
        if (name in REDIRECTING or name.startswith("GIT_TRACE") or name.startswith("GIT_CONFIG_KEY_")
                or name.startswith("GIT_CONFIG_VALUE_")):
            del os.environ[name]


class GitCase(unittest.TestCase):
    """Base class: temporary folder, and a Git that ignores the global and system configuration."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        patcher = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CEILING_DIRECTORIES": str(self.base),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        clear_git_environment()
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.base)

    def git(self, repo, *args):
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, "git " + " ".join(args) + ": " + result.stderr)
        return result.stdout

    def make_repo(self, name="repo"):
        repo = self.base / name
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.name", "Test")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "commit.gpgsign", "false")
        return repo

    def commit_all(self, repo, message="c"):
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", message)

    def seeded_repo(self, name="repo"):
        repo = self.make_repo(name)
        (repo / ".gitignore").write_text("*.log\n")
        (repo / "a.txt").write_text("one\n")
        (repo / "c.txt").write_text("two\n")
        (repo / "run.sh").write_text("#!/bin/sh\n")
        os.symlink("a.txt", repo / "link")
        (repo / "tracked.log").write_text("tracked but ignored\n")
        self.git(repo, "add", "-f", "tracked.log")
        self.commit_all(repo)
        return repo

    def ok(self, repo, seconds=30):
        result = review_capture.capture(str(repo), seconds)
        self.assertEqual(result["state"], "ok", result)
        return result


class IdentityTests(GitCase):
    def test_identity_matches_an_independent_computation(self):
        repo = self.seeded_repo()
        (repo / "new.txt").write_text("untracked\n")
        (repo / "skip.log").write_text("ignored\n")
        got = self.ok(repo)
        names = sorted(["tracked.log", ".gitignore", "a.txt", "c.txt", "run.sh", "link", "new.txt"], key=str.encode)
        records = []
        for name in names:
            path = repo / name
            if path.is_symlink():
                target = os.readlink(path).encode()
                records.append((b"l", b"-", sha(target).encode(), str(len(target)).encode(), name.encode()))
            else:
                data = path.read_bytes()
                exe = b"x" if os.stat(path).st_mode & 0o100 else b"-"
                records.append((b"f", exe, sha(data).encode(), str(len(data)).encode(), name.encode()))
        self.assertEqual(got["manifest_sha256"], expected_identity(records))
        self.assertEqual(got["entries"], 7)
        self.assertEqual(got["root"], str(repo))
        self.assertEqual(len(got["index_sha256"]), 64)
        self.assertIsNone(got["reason"])
        self.assertEqual(got["manifest_version"], 1)

    def test_same_identity_across_a_commit_of_part_of_the_content(self):
        repo = self.seeded_repo()
        (repo / "a.txt").write_text("one changed\n")
        (repo / "c.txt").write_text("two changed\n")
        before = self.ok(repo)
        self.git(repo, "add", "a.txt")
        self.git(repo, "commit", "-q", "-m", "part")
        after = self.ok(repo)
        self.assertEqual(before["manifest_sha256"], after["manifest_sha256"])
        self.assertNotEqual(before["index_sha256"], after["index_sha256"])

    def test_same_identity_across_a_change_to_an_ignored_untracked_file(self):
        repo = self.seeded_repo()
        (repo / "skip.log").write_text("v1\n")
        before = self.ok(repo)
        (repo / "skip.log").write_text("v2 and longer\n")
        (repo / "other.log").write_text("new ignored file\n")
        after = self.ok(repo)
        self.assertEqual(before["manifest_sha256"], after["manifest_sha256"])
        self.assertEqual(before["entries"], after["entries"])

    def test_changed_tracked_file_changes_the_identity(self):
        repo = self.seeded_repo()
        before = self.ok(repo)
        (repo / "a.txt").write_text("one!\n")
        self.assertNotEqual(self.ok(repo)["manifest_sha256"], before["manifest_sha256"])

    def test_same_size_change_of_a_tracked_file_changes_the_identity(self):
        repo = self.seeded_repo()
        before = self.ok(repo)
        (repo / "a.txt").write_text("ONE\n")
        self.assertNotEqual(self.ok(repo)["manifest_sha256"], before["manifest_sha256"])

    def test_new_untracked_file_changes_the_identity(self):
        repo = self.seeded_repo()
        before = self.ok(repo)
        (repo / "fresh.txt").write_text("")
        after = self.ok(repo)
        self.assertNotEqual(after["manifest_sha256"], before["manifest_sha256"])
        self.assertEqual(after["entries"], before["entries"] + 1)

    def test_deleted_tracked_file_changes_the_identity(self):
        repo = self.seeded_repo()
        before = self.ok(repo)
        (repo / "c.txt").unlink()
        after = self.ok(repo)
        self.assertNotEqual(after["manifest_sha256"], before["manifest_sha256"])
        self.assertEqual(after["entries"], before["entries"])

    def test_deleted_untracked_file_changes_the_identity(self):
        repo = self.seeded_repo()
        (repo / "fresh.txt").write_text("x")
        before = self.ok(repo)
        (repo / "fresh.txt").unlink()
        self.assertNotEqual(self.ok(repo)["manifest_sha256"], before["manifest_sha256"])

    def test_changed_executable_bit_changes_the_identity(self):
        # Only S_IXUSR changes (0o644 -> 0o744), with core.filemode=false so that Git itself would not notice.
        repo = self.seeded_repo()
        self.git(repo, "config", "core.filemode", "false")
        before = self.ok(repo)
        mode = os.stat(repo / "run.sh").st_mode & 0o7777
        self.assertEqual(mode & 0o111, 0)
        os.chmod(repo / "run.sh", mode | stat.S_IXUSR)
        self.assertEqual((os.stat(repo / "run.sh").st_mode & 0o7777) ^ mode, stat.S_IXUSR)
        changed = self.ok(repo)
        self.assertNotEqual(changed["manifest_sha256"], before["manifest_sha256"])
        os.chmod(repo / "run.sh", mode)
        self.assertEqual(self.ok(repo)["manifest_sha256"], before["manifest_sha256"])

    def test_group_and_other_execute_bits_do_not_change_the_identity(self):
        repo = self.seeded_repo()
        self.git(repo, "config", "core.filemode", "false")
        before = self.ok(repo)
        mode = os.stat(repo / "run.sh").st_mode & 0o7777
        os.chmod(repo / "run.sh", mode | stat.S_IXGRP | stat.S_IXOTH)
        self.assertEqual((os.stat(repo / "run.sh").st_mode & 0o7777) ^ mode, stat.S_IXGRP | stat.S_IXOTH)
        self.assertEqual(self.ok(repo)["manifest_sha256"], before["manifest_sha256"])

    def test_clear_git_environment_removes_trace_and_command_scope_variables(self):
        extra = {"GIT_TRACE": "/x", "GIT_TRACE2_EVENT": "/y", "GIT_TRACE_SETUP": "1", "GIT_CONFIG_COUNT": "1",
                 "GIT_CONFIG_KEY_0": "core.x", "GIT_CONFIG_VALUE_0": "y", "GIT_CONFIG_PARAMETERS": "'a=b'",
                 "GIT_DIR": "/z"}
        with mock.patch.dict(os.environ, extra):
            clear_git_environment()
            for name in extra:
                self.assertNotIn(name, os.environ)
        self.assertFalse([n for n in os.environ if n.startswith("GIT_TRACE") or n.startswith("GIT_CONFIG_KEY_")])

    def test_changed_symlink_target_changes_the_identity(self):
        repo = self.seeded_repo()
        before = self.ok(repo)
        (repo / "link").unlink()
        os.symlink("c.txt", repo / "link")
        self.assertNotEqual(self.ok(repo)["manifest_sha256"], before["manifest_sha256"])

    def test_symlink_is_not_followed(self):
        repo = self.seeded_repo()
        before = self.ok(repo)
        (repo / "dangling").symlink_to("does-not-exist")
        self.assertNotEqual(self.ok(repo)["manifest_sha256"], before["manifest_sha256"])

    def test_changed_tracked_file_that_matches_an_ignore_pattern_changes_the_identity(self):
        repo = self.seeded_repo()
        before = self.ok(repo)
        (repo / "tracked.log").write_text("changed under an ignore pattern\n")
        self.assertNotEqual(self.ok(repo)["manifest_sha256"], before["manifest_sha256"])

    def test_line_ending_filter_does_not_hide_a_byte_change(self):
        repo = self.seeded_repo()
        self.git(repo, "config", "core.autocrlf", "true")
        before = self.ok(repo)
        (repo / "a.txt").write_bytes(b"one\r\n")
        self.assertNotEqual(self.ok(repo)["manifest_sha256"], before["manifest_sha256"])

    def test_assume_unchanged_does_not_hide_a_byte_change(self):
        repo = self.seeded_repo()
        self.git(repo, "update-index", "--assume-unchanged", "a.txt")
        before = self.ok(repo)
        (repo / "a.txt").write_text("hidden from Git\n")
        self.assertNotEqual(self.ok(repo)["manifest_sha256"], before["manifest_sha256"])

    def test_capture_from_a_subfolder_names_the_whole_worktree(self):
        repo = self.seeded_repo()
        (repo / "sub").mkdir()
        (repo / "sub" / "n.txt").write_text("n")
        self.assertEqual(self.ok(repo / "sub")["manifest_sha256"], self.ok(repo)["manifest_sha256"])
        self.assertEqual(self.ok(repo / "sub")["root"], str(repo))

    def redirecting_env(self, other):
        return {
            "GIT_DIR": str(other / ".git"), "GIT_INDEX_FILE": str(other / ".git" / "index"),
            "GIT_WORK_TREE": str(other), "GIT_COMMON_DIR": str(other / ".git"),
            "GIT_OBJECT_DIRECTORY": str(other / ".git" / "objects"), "GIT_NAMESPACE": "ns",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other / ".git" / "objects"),
        }

    def test_redirecting_environment_does_not_change_the_capture(self):
        repo = self.seeded_repo()
        other = self.make_repo("other")
        (other / "only-here.txt").write_text("other\n")
        self.commit_all(other)
        before = self.ok(repo)
        with mock.patch.dict(os.environ, self.redirecting_env(other)):
            after = self.ok(repo)
        self.assertEqual(after, before)
        self.assertEqual(after["root"], str(repo))

    def test_git_ceiling_directories_does_not_change_the_capture(self):
        repo = self.seeded_repo()
        (repo / "sub").mkdir()
        before = self.ok(repo / "sub")
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(repo)}):
            self.assertEqual(self.ok(repo / "sub"), before)

    def test_redirecting_environment_does_not_change_git_common_dir(self):
        repo = self.seeded_repo()
        other = self.make_repo("other")
        before = review_capture.git_common_dir(str(repo))
        self.assertEqual(before, str(repo / ".git"))
        with mock.patch.dict(os.environ, self.redirecting_env(other)):
            self.assertEqual(review_capture.git_common_dir(str(repo)), before)

    def test_git_calls_get_a_scrubbed_environment(self):
        repo = self.seeded_repo()
        real = subprocess.run
        seen = []

        def record(cmd, *args, **kwargs):
            seen.append(kwargs["env"])
            return real(cmd, *args, **kwargs)
        with mock.patch.dict(os.environ, dict(self.redirecting_env(repo), GIT_TRACE_PACKET="1", GIT_TRACE="/x")), \
                mock.patch.object(review_capture.subprocess, "run", record):
            self.ok(repo)
            review_capture.git_common_dir(str(repo))
        self.assertGreaterEqual(len(seen), 5)
        for env in seen:
            for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                         "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
                         "GIT_CEILING_DIRECTORIES"):
                self.assertNotIn(name, env)
            self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
            self.assertEqual(env["GIT_TRACE"], "0")
            self.assertNotIn("GIT_TRACE_PACKET", env)


class FakeGit:
    """Stands in for review_capture._git on the fixture tree. It keeps the budget rule of the real function.

    It accepts only the four exact commands of the capture. Any other argument list fails the test.
    """

    REV_PARSE = ["rev-parse", "--show-toplevel"]
    INDEX = ["ls-files", "-s", "-z"]
    CACHED = ["ls-files", "-z", "--cached"]
    OTHERS = ["ls-files", "-z", "--others", "--exclude-standard"]

    def __init__(self, cached=FIXTURE_PATHS, others=(), index=b"", cached2=None, others2=None, on_listing2=None):
        self.cached = [list(cached), list(cached if cached2 is None else cached2)]
        self.others = [list(others), list(others if others2 is None else others2)]
        self.index = index
        self.counts = {"cached": 0, "others": 0}
        self.on_listing2 = on_listing2

    def __call__(self, cwd, args, deadline):
        review_capture._check(deadline)
        if args == self.REV_PARSE:
            return 0, os.fsencode(str(FIXTURE)) + b"\n"
        if args not in (self.INDEX, self.CACHED, self.OTHERS):
            raise AssertionError("unexpected git arguments: %r" % (args,))
        if os.fsdecode(cwd) != str(FIXTURE):
            raise AssertionError("ls-files must run at the worktree root, not in %r" % (cwd,))
        if args == self.INDEX:
            return 0, self.index
        kind = "cached" if args == self.CACHED else "others"
        self.counts[kind] += 1
        number = min(self.counts[kind], 2) - 1
        if number == 1 and kind == "others" and self.on_listing2:
            self.on_listing2()
        paths = (self.cached if kind == "cached" else self.others)[number]
        return 0, b"".join(path + b"\0" for path in paths)


class FakeRun:
    """Stands in for subprocess.run under the real review_capture._git and git_common_dir. No process starts.

    overrides maps (key, occurrence number) to (return code, stdout) or to an exception that is raised.
    Keys: rev-parse, index, cached, others, common. The fake checks the command prefix, the keyword arguments
    and the scrubbed environment, and records (key, cwd, timeout) of every call.
    """

    PREFIX = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C"]

    def __init__(self, overrides=None, cached=FIXTURE_PATHS, others=()):
        self.overrides = overrides or {}
        self.paths = {"cached": list(cached), "others": list(others)}
        self.counts = {}
        self.calls = []

    @staticmethod
    def key(args):
        keys = {("rev-parse", "--show-toplevel"): "rev-parse", ("rev-parse", "--git-common-dir"): "common",
                ("ls-files", "-s", "-z"): "index", ("ls-files", "-z", "--cached"): "cached",
                ("ls-files", "-z", "--others", "--exclude-standard"): "others"}
        if tuple(args) not in keys:
            raise AssertionError("unexpected git arguments: %r" % (args,))
        return keys[tuple(args)]

    def __call__(self, cmd, **kwargs):
        assert cmd[:5] == self.PREFIX, cmd
        cwd, args = cmd[5], cmd[6:]
        for name, value in (("capture_output", True), ("check", False), ("stdin", subprocess.DEVNULL)):
            assert kwargs[name] == value, (name, kwargs)
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0" and kwargs["env"]["GIT_TRACE"] == "0"
        key = self.key(args)
        if key in ("index", "cached", "others"):
            assert cwd == str(FIXTURE), "ls-files must run at the worktree root: " + cwd
        number = self.counts[key] = self.counts.get(key, 0) + 1
        self.calls.append((key, cwd, kwargs["timeout"]))
        if (key, number) in self.overrides:
            answer = self.overrides[(key, number)]
            if isinstance(answer, BaseException):
                raise answer
            code, out = answer
        else:
            code, out = 0, {"rev-parse": os.fsencode(str(FIXTURE)) + b"\n", "common": b".git\n", "index": b"",
                            "cached": b"".join(p + b"\0" for p in self.paths["cached"]),
                            "others": b"".join(p + b"\0" for p in self.paths["others"])}[key]
        return subprocess.CompletedProcess(cmd, code, out, b"")


class OsProxy:
    """Stands in for the os module inside review_capture: some names removed, some replaced."""

    def __init__(self, removed=(), **replaced):
        self._removed = set(removed)
        self._replaced = replaced

    def __getattr__(self, name):
        if name in self._removed:
            raise AttributeError(name)
        if name in self._replaced:
            return self._replaced[name]
        return getattr(os, name)


def lstat_script(script):
    """Wrap os.lstat. script(name, call number, real result or None) returns a replacement, raises, or returns None."""
    calls = {}

    def fake(path):
        name = os.path.relpath(os.fsdecode(path), str(FIXTURE))
        calls[name] = calls.get(name, 0) + 1
        real, error = None, None
        try:
            real = os.lstat(path)
        except OSError as exc:
            error = exc
        result = script(name, calls[name], real)
        if result is not None:
            return result
        if error is not None:
            raise error
        return real
    return fake


class StateTests(unittest.TestCase):
    """Every state of ACC-3 through injection on the fixture tree or on this repository. Writes nothing."""

    def cap(self, git=None, seconds=30, **attrs):
        with patched(_git=git or FakeGit(), **attrs):
            return review_capture.capture(str(FIXTURE), seconds)

    def assert_no_identity(self, result, state, reason):
        self.assertEqual(result["state"], state, result)
        self.assertIsNone(result["manifest_sha256"])
        self.assertIsNone(result["entries"])
        self.assertIsInstance(result["reason"], str)
        self.assertEqual(result["manifest_version"], 1)
        self.assertEqual(result["reason"], reason)

    def cap_run(self, run, seconds=30, **attrs):
        """Capture the fixture with the real _git and an injected subprocess.run."""
        with mock.patch.object(review_capture.subprocess, "run", run), patched(**attrs):
            return review_capture.capture(str(FIXTURE), seconds)

    def test_baseline_with_the_real_git_wrapper_and_an_injected_run(self):
        run = FakeRun()
        result = self.cap_run(run)
        self.assertEqual(result["state"], "ok", result)
        self.assertIsNone(result["reason"])
        self.assertEqual(result["root"], str(FIXTURE))
        self.assertEqual([call[0] for call in run.calls], ["rev-parse", "index", "cached", "others", "cached", "others"])
        self.assertEqual(result["manifest_sha256"], self.cap()["manifest_sha256"])

    def test_each_git_call_that_fails_alone_gives_error(self):
        # ls-files -s, and each of the first and the second --cached and --others listings, one at a time.
        for key, number in (("index", 1), ("cached", 1), ("others", 1), ("cached", 2), ("others", 2)):
            for out in (b"", b"plain.txt\0"):
                with self.subTest(call=key, number=number, output=out):
                    result = self.cap_run(FakeRun({(key, number): (128, out)}))
                    self.assert_no_identity(result, "error", "git ls-files failed")

    def test_top_level_output_without_a_trailing_newline_is_the_root(self):
        result = self.cap_run(FakeRun({("rev-parse", 1): (0, os.fsencode(str(FIXTURE)))}))
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual(result["root"], str(FIXTURE))
        self.assertEqual(self.cap_run(FakeRun())["root"], str(FIXTURE))  # and with the newline

    def test_top_level_failure_with_output_and_top_level_empty_output_give_not_git(self):
        for label, answer in (("exit 128 with output", (128, os.fsencode(str(FIXTURE)) + b"\n")),
                              ("exit 1 with output, no newline", (1, os.fsencode(str(FIXTURE)))),
                              ("exit 0 with empty output", (0, b"")), ("exit 0 with a newline only", (0, b"\n"))):
            with self.subTest(label):
                self.assert_no_identity(self.cap_run(FakeRun({("rev-parse", 1): answer})), "not-git",
                                        "not inside a Git worktree")

    def test_git_that_cannot_start_or_does_not_answer(self):
        self.assert_no_identity(self.cap_run(FakeRun({("rev-parse", 1): FileNotFoundError("git")})), "not-git",
                                "git is not available")
        self.assert_no_identity(self.cap_run(FakeRun({("cached", 1): subprocess.TimeoutExpired("git", 1)})), "timeout",
                                "git did not answer in time")

    def test_untracked_listed_path_that_does_not_exist_gives_error(self):
        # Only a tracked path may be absent (type a). An untracked listed path that is gone is an error.
        self.assert_no_identity(self.cap(FakeGit(others=[b"ghost.txt"])), "error", "a path vanished during capture")
        self.assertEqual(self.cap(FakeGit(cached=FIXTURE_PATHS + [b"ghost.txt"]))["state"], "ok")

    def test_descriptor_that_is_not_a_regular_file_gives_error_cannot_read(self):
        real = os.fstat
        for kind in (stat.S_IFIFO, stat.S_IFDIR, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK):
            with self.subTest(kind=kind):
                def fstat(fd, kind=kind):
                    return stat_like(real(fd), st_mode=kind | 0o644)
                self.assert_no_identity(self.cap(_fstat=fstat), "error", "cannot read a file")

    def test_open_flags_are_read_only_no_follow_and_non_blocking(self):
        flags_seen = []

        def opener(path, flags):
            flags_seen.append(flags)
            return os.open(path, flags)
        plain = os.fsencode(str(FIXTURE / "plain.txt"))
        expected = os.lstat(plain)
        full = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        cases = [((), full), (("O_NOFOLLOW",), os.O_RDONLY | os.O_NONBLOCK),
                 (("O_NONBLOCK",), os.O_RDONLY | os.O_NOFOLLOW), (("O_NOFOLLOW", "O_NONBLOCK"), os.O_RDONLY)]
        for removed, flags in cases:
            with self.subTest(removed=removed):
                flags_seen.clear()
                with patched(_open=opener, os=OsProxy(removed=removed)):
                    digest, count = review_capture._read_file(plain, expected, lambda: None)
                self.assertEqual(flags_seen, [flags])
                self.assertEqual((digest, count), (sha(b"plain\n"), 6))
        self.assertEqual(os.O_RDONLY, 0)

    def memory_read(self, data, check):
        """Run _read_file on `data` held in memory, with the module's CHUNK untouched. No file is opened."""
        import io

        class MemoryFile(io.BytesIO):
            def fileno(self):
                return 99
        expected = types.SimpleNamespace(st_dev=1, st_ino=2, st_size=len(data), st_mtime_ns=3,
                                         st_mode=stat.S_IFREG | 0o644)
        with patched(_open=lambda path, flags: 99, _fstat=lambda fd: expected,
                     os=OsProxy(fdopen=lambda fd, *args, **kwargs: MemoryFile(data))):
            return review_capture._read_file(b"in-memory", expected, check)

    def test_one_chunk_plus_one_byte_has_three_read_loop_checks(self):
        # 1048577 bytes = the module's chunk (1 << 20) + 1. The reads return 1048576 bytes, 1 byte, then nothing,
        # so the check before each read runs exactly three times. A larger chunk reads everything at once: two checks.
        data = b"x" * ((1 << 20) + 1)
        self.assertEqual(review_capture.CHUNK, 1 << 20)
        calls = []
        digest, count = self.memory_read(data, lambda: calls.append(1))
        self.assertEqual((digest, count), (sha(data), len(data)))
        self.assertEqual(len(calls), 3)

    def test_a_clock_at_the_deadline_at_the_third_read_loop_check_stops_the_read(self):
        data = b"x" * ((1 << 20) + 1)
        for value in (5.0, 100.0):
            with self.subTest(clock=value):
                now = [0.0]
                checks = [0]

                def check():
                    checks[0] += 1
                    if checks[0] == 3:
                        now[0] = value  # the clock reaches or passes the deadline (5.0) at the third check
                    review_capture._check(5.0)
                with patched(_clock=lambda: now[0]):
                    with self.assertRaises(review_capture._Stop) as raised:
                        self.memory_read(data, check)
                self.assertEqual((raised.exception.state, raised.exception.reason), ("timeout", "time budget used up"))
                self.assertEqual(checks[0], 3)
        now = [0.0]
        with patched(_clock=lambda: now[0]):  # the clock never reaches the deadline: no stop
            self.assertEqual(self.memory_read(data, lambda: review_capture._check(5.0))[1], len(data))

    def test_file_is_read_unbuffered_in_binary_mode(self):
        seen = []

        def fdopen(fd, *args, **kwargs):
            seen.append((args, kwargs))
            return os.fdopen(fd, *args, **kwargs)
        plain = os.fsencode(str(FIXTURE / "plain.txt"))
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # buffering=1 in binary mode would warn
            with patched(os=OsProxy(fdopen=fdopen)):
                review_capture._read_file(plain, os.lstat(plain), lambda: None)
        self.assertEqual(seen, [(("rb",), {"buffering": 0})])

    def test_baseline_on_the_fixture_is_ok(self):
        result = self.cap()
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual(result["entries"], 4)
        self.assertEqual(result["root"], str(FIXTURE))

    def test_gitlink_in_the_index(self):
        index = b"100644 " + b"a" * 40 + b" 0\tplain.txt\0" + b"160000 " + b"b" * 40 + b" 0\tsub\0"
        result = self.cap(FakeGit(index=index))
        self.assert_no_identity(result, "unsupported", "gitlink in the index")
        self.assertEqual(result["index_sha256"], sha(index))

    def test_directory_in_the_path_list(self):
        self.assert_no_identity(self.cap(FakeGit(cached=FIXTURE_PATHS + [b"sub"])), "unsupported", "directory or nested repository in the path list")

    def test_fifo_socket_and_device_modes_from_an_injected_stat(self):
        for kind in (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK):
            with self.subTest(kind=kind):
                def script(name, n, real):
                    if name == "plain.txt" and n == 1:
                        return stat_like(real, st_mode=kind | 0o644)
                with patched(_lstat=lstat_script(script)):
                    self.assert_no_identity(self.cap(), "unsupported", "path is neither a regular file nor a symlink")

    def test_descriptor_metadata_differs_from_the_path_stat(self):
        real = os.fstat
        for number in (1, 2):  # 1: right after the open, 2: after the read, on the same descriptor
            for field, value in (("st_ino", 1), ("st_size", 1), ("st_mtime_ns", 1), ("st_dev", 1), ("st_mode", 1)):
                with self.subTest(call=number, field=field):
                    calls = {}
                    real_open = os.open

                    def opened(path, flags):
                        fd = real_open(path, flags)
                        calls[fd] = 0  # a reused descriptor number starts a new count
                        return fd

                    def fstat(fd):
                        calls[fd] = calls.get(fd, 0) + 1
                        info = real(fd)
                        if calls[fd] == number:  # the number-th call on one descriptor
                            return stat_like(info, **{field: getattr(info, field) ^ value})
                        return info
                    self.assert_no_identity(self.cap(_fstat=fstat, _open=opened), "error", "changed during capture")

    def test_path_metadata_changes_between_the_first_stat_and_the_read_check(self):
        def script(name, n, real):
            if name == "plain.txt" and n == 2:
                return stat_like(real, st_mtime_ns=real.st_mtime_ns + 1)
        with patched(_lstat=lstat_script(script)):
            self.assert_no_identity(self.cap(), "error", "changed during capture")

    def test_path_metadata_changes_at_the_read_check_for_every_kind_of_path(self):
        for name in ("link", "plain.txt", "run.sh", "sub/inner.txt"):
            for field in ("st_dev", "st_mtime_ns", "st_ino", "st_size", "st_mode"):
                with self.subTest(path=name, field=field):
                    def script(n, c, real, name=name, field=field):
                        if n == name and c == 2:
                            return stat_like(real, **{field: getattr(real, field) ^ 1})
                    with patched(_lstat=lstat_script(script)):
                        self.assert_no_identity(self.cap(), "error", "changed during capture")

    def test_final_recheck_finds_a_changed_path(self):
        for name in ("link", "plain.txt", "run.sh", "sub/inner.txt"):
            for field in ("st_dev", "st_mtime_ns", "st_ino", "st_size", "st_mode"):
                with self.subTest(path=name, field=field):
                    def script(n, c, real, name=name, field=field):
                        if n == name and c == 3:
                            return stat_like(real, **{field: getattr(real, field) ^ 1})
                    with patched(_lstat=lstat_script(script)):
                        self.assert_no_identity(self.cap(), "error", "changed during capture")

    def test_final_recheck_finds_a_path_that_became_absent(self):
        def script(name, n, real):
            if name == "plain.txt" and n == 3:
                raise FileNotFoundError(name)
        with patched(_lstat=lstat_script(script)):
            self.assert_no_identity(self.cap(), "error", "changed during capture")

    def test_final_recheck_finds_a_path_that_became_present(self):
        paths = FIXTURE_PATHS + [b"ghost.txt"]
        self.assertEqual(self.cap(FakeGit(cached=paths))["state"], "ok")
        present = os.lstat(FIXTURE / "plain.txt")

        def script(name, n, real):
            if name == "ghost.txt" and n == 2:
                return present
        with patched(_lstat=lstat_script(script)):
            self.assert_no_identity(self.cap(FakeGit(cached=paths)), "error", "changed during capture")

    def test_path_list_that_differs_at_the_end(self):
        # A path added to, or removed from, either listing between the first and the second listing.
        split = dict(cached=[b"link", b"plain.txt", b"run.sh"], others=[b"sub/inner.txt"])
        cases = {
            "path added to the cached listing": dict(cached2=FIXTURE_PATHS + [b"extra.txt"]),
            "path removed from the cached listing": dict(cached2=FIXTURE_PATHS[:-1]),
            "path added to the others listing": dict(others2=[b"new.txt"]),
            "path added to the others listing (split fixture)": dict(split, others2=[b"sub/inner.txt", b"new.txt"]),
            "path removed from the others listing (split fixture)": dict(split, others2=[]),
            "path removed from the cached listing (split fixture)": dict(split, cached2=[b"link", b"plain.txt"]),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                self.assert_no_identity(self.cap(FakeGit(**kwargs)), "error", "path list changed during capture")

    def test_path_that_moves_between_the_two_listings(self):
        # Removed from one listing and added to the other: the union stays equal, each listing differs.
        split = dict(cached=[b"link", b"plain.txt", b"run.sh"], others=[b"sub/inner.txt"])
        cases = {
            "cached to others": dict(cached2=FIXTURE_PATHS[:-1], others2=[b"sub/inner.txt"]),
            "others to cached": dict(split, cached2=FIXTURE_PATHS, others2=[]),
            "cached to others (split fixture)": dict(split, cached2=[b"link", b"plain.txt"], others2=[b"sub/inner.txt", b"run.sh"]),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                git = FakeGit(**kwargs)
                self.assertEqual(sorted(set(git.cached[1]) | set(git.others[1])), FIXTURE_PATHS)  # the union is unchanged
                self.assert_no_identity(self.cap(git), "error", "path list changed during capture")

    def test_unchanged_second_listing_is_ok(self):
        git = FakeGit(others=[b"sub/inner.txt"], cached=[b"link", b"plain.txt", b"run.sh"])
        self.assertEqual(self.cap(git)["state"], "ok")
        self.assertEqual(git.counts, {"cached": 2, "others": 2})

    def test_a_clock_that_expires_at_any_check_gives_timeout(self):
        def run(expire_after):
            calls = [0]

            def clock():
                calls[0] += 1
                return 0.0 if calls[0] <= expire_after else 100.0
            return self.cap(seconds=5, _clock=clock), calls[0]
        good, total = run(10 ** 9)
        self.assertEqual(good["state"], "ok", good)
        self.assertGreater(total, 20)
        for expire_after in range(1, total):
            result, _ = run(expire_after)
            self.assert_no_identity(result, "timeout", "time budget used up")

    POINTS = {
        "capture": "the start: the deadline is set",
        "_check<-__call__": "before each Git call (FakeGit repeats the budget rule of the real _git)",
        "_check<-_collect": "before each file, before each recheck, and after the last recheck",
        "_check<-<lambda>": "before each chunk of a read",
        "_check<-_capture": "the final check, after the manifest hash",
    }

    @staticmethod
    def clock_point(depth):
        """Name the code place that called the clock: the function and its caller (capture itself has no caller name)."""
        frame = sys._getframe(depth)
        name = frame.f_code.co_name
        return "capture" if name == "capture" else name + "<-" + frame.f_back.f_code.co_name

    def test_every_check_point_of_the_clock_is_named_and_the_last_is_after_the_hash(self):
        events = []
        real_digest = review_capture.manifest_digest

        def clock():
            events.append(self.clock_point(2))
            return 0.0

        def digest(entries):
            events.append("HASH")
            return real_digest(entries)
        result = self.cap(seconds=5, _clock=clock, manifest_digest=digest)
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual(set(events) - {"HASH"}, set(self.POINTS), events)
        self.assertEqual(events[-2:], ["HASH", "_check<-_capture"])
        self.assertEqual(events.count("HASH"), 1)

    POINTS_REAL = dict(
        {k: v for k, v in POINTS.items() if k != "_check<-__call__"},
        **{"_git<-_capture": "the real _git, before the top level and index calls",
           "_git<-_listings": "the real _git, before each listing"})

    def point_clock(self, point, value, hits):
        """The first call returns 0.0, so the deadline is exactly 0.0 + 5 = 5.0. A call at `point` returns value."""
        def clock():
            if self.clock_point(2) == point:
                hits[0] += 1
                return value
            return 0.0
        return clock

    def test_a_clock_at_just_before_and_past_the_deadline_at_each_named_check_point(self):
        # deadline = 5.0. "at" is 5.0, "past" is 100.0, "just before" is 4.999 (ok).
        for point, meaning in self.POINTS.items():
            if point == "capture":
                continue  # the first call fixes the deadline; it cannot be past it
            for value, state in ((5.0, "timeout"), (100.0, "timeout"), (4.999, "ok")):
                hits = [0]
                with self.subTest(check_point=point, meaning=meaning, clock=value):
                    result = self.cap(seconds=5, _clock=self.point_clock(point, value, hits))
                    if state == "ok":
                        self.assertEqual(result["state"], "ok", result)
                        self.assertGreaterEqual(hits[0], 1)
                    else:
                        self.assert_no_identity(result, "timeout", "time budget used up")
                        self.assertEqual(hits[0], 1)  # the capture stopped at the first such call at this point

    def test_the_real_git_check_at_just_before_and_past_the_deadline(self):
        # Same numbers, with the real _git and an injected subprocess.run. The run must get the time left.
        timeouts = {"_git<-_capture": ("rev-parse", "index"), "_git<-_listings": ("cached", "others")}
        for point, meaning in self.POINTS_REAL.items():
            if point == "capture":
                continue
            for value, state in ((5.0, "timeout"), (100.0, "timeout"), (4.999, "ok")):
                hits = [0]
                run = FakeRun()
                with self.subTest(check_point=point, meaning=meaning, clock=value):
                    result = self.cap_run(run, seconds=5, _clock=self.point_clock(point, value, hits))
                    if state == "ok":
                        self.assertEqual(result["state"], "ok", result)
                        self.assertGreaterEqual(hits[0], 1)
                        for key, _, timeout in run.calls:
                            if point in timeouts and key in timeouts[point]:
                                self.assertAlmostEqual(timeout, 5.0 - 4.999, places=6)  # no process got more time
                    else:
                        self.assert_no_identity(result, "timeout", "time budget used up")
                        self.assertEqual(hits[0], 1)
                        if point == "_git<-_capture":  # the first Git call expired: no process started
                            self.assertEqual(run.calls, [])
                        if point == "_git<-_listings":  # the first listing expired: only the two earlier calls ran
                            self.assertEqual([call[0] for call in run.calls], ["rev-parse", "index"])

    def test_every_check_point_of_the_real_git_variant_is_named(self):
        events = []

        def clock():
            events.append(self.clock_point(2))
            return 0.0
        result = self.cap_run(FakeRun(), seconds=5, _clock=clock)
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual(set(events), set(self.POINTS_REAL), events)

    def test_clock_jumps_during_the_final_lstat(self):
        now = [0.0]

        def script(name, n, real):
            if name == "sub/inner.txt" and n == 3:
                now[0] = 100.0
        with patched(_lstat=lstat_script(script)):
            self.assert_no_identity(self.cap(seconds=5, _clock=lambda: now[0]), "timeout", "time budget used up")

    def test_clock_jumps_during_the_last_read(self):
        now = [0.0]

        def script(name, n, real):
            if name == "sub/inner.txt" and n == 2:
                now[0] = 100.0
        with patched(_lstat=lstat_script(script)):
            self.assert_no_identity(self.cap(seconds=5, _clock=lambda: now[0]), "timeout", "time budget used up")

    def test_clock_jumps_during_the_second_listing(self):
        now = [0.0]
        git = FakeGit(on_listing2=lambda: now.__setitem__(0, 100.0))
        self.assert_no_identity(self.cap(git, seconds=5, _clock=lambda: now[0]), "timeout", "time budget used up")

    def test_clock_jumps_during_the_hash(self):
        now = [0.0]
        real = review_capture.manifest_digest

        def digest(entries):
            result = real(entries)
            now[0] = 100.0
            return result
        self.assert_no_identity(self.cap(seconds=5, _clock=lambda: now[0], manifest_digest=digest), "timeout", "time budget used up")

    def test_zero_budget_gives_timeout_before_any_git_call(self):
        with mock.patch.object(review_capture.subprocess, "run", side_effect=AssertionError("git ran")):
            self.assert_no_identity(review_capture.capture(str(ROOT), 0), "timeout", "time budget used up")

    def test_git_call_gets_a_timeout_no_longer_than_the_time_left(self):
        seen = []

        def run(cmd, *args, **kwargs):
            seen.append(kwargs["timeout"])
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        with patched(_clock=lambda: 10.0), mock.patch.object(review_capture.subprocess, "run", run):
            result = review_capture.capture(str(ROOT), 3)
        self.assert_no_identity(result, "timeout", "git did not answer in time")
        self.assertEqual(seen, [3.0])

    def test_unreadable_file_gives_error(self):
        def refuse(path, expected, check):
            raise PermissionError("refused")
        self.assert_no_identity(self.cap(_read_file=refuse), "error", "cannot read a file")

    def test_file_that_grows_during_the_read(self):
        real = review_capture._read_file

        def grown(path, expected, check):
            digest, count = real(path, expected, check)
            return digest, count + 1
        self.assert_no_identity(self.cap(_read_file=grown), "error", "changed during capture")

    def test_failing_git_command_gives_error(self):
        root_call = FakeGit()

        def git(cwd, args, deadline):
            if args[0] == "ls-files" and args[1] != "-s":
                return 128, b""
            return root_call(cwd, args, deadline)
        self.assert_no_identity(self.cap(git), "error", "git ls-files failed")

    def test_folder_outside_git(self):
        result = review_capture.capture("/", 30)
        self.assert_no_identity(result, "not-git", "not inside a Git worktree")
        self.assertIsNone(result["root"])
        self.assertIsNone(result["index_sha256"])

    def test_folder_that_does_not_exist(self):
        self.assert_no_identity(review_capture.capture("/nonexistent-review-capture-folder", 30), "not-git", "not inside a Git worktree")

    def test_git_not_available(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent-bin"}):
            self.assert_no_identity(review_capture.capture("/", 30), "not-git", "git is not available")

    def test_git_folder_itself_is_not_a_worktree(self):
        git_dir = os.fsdecode(git_out(ROOT, "rev-parse", "--absolute-git-dir")).strip()
        self.assert_no_identity(review_capture.capture(git_dir, 30), "not-git", "not inside a Git worktree")


class RepoStateTests(GitCase):
    """The states that a real repository can show. Needs a writable temporary folder."""

    def assert_no_identity(self, result, state, reason):
        self.assertEqual(result["state"], state, result)
        self.assertIsNone(result["manifest_sha256"])
        self.assertIsNone(result["entries"])
        self.assertEqual(result["reason"], reason)

    def test_real_gitlink_in_the_index(self):
        repo = self.seeded_repo()
        head = self.git(repo, "rev-parse", "HEAD").strip()
        self.git(repo, "update-index", "--add", "--cacheinfo", "160000," + head + ",sub")
        self.assert_no_identity(review_capture.capture(str(repo), 30), "unsupported", "gitlink in the index")

    def test_real_untracked_nested_repository(self):
        # A nested repository that Git lists: `git ls-files --others` shows it as a directory entry "inner/".
        repo = self.seeded_repo()
        nested = self.make_repo("nested-tmp")
        (nested / "f.txt").write_text("f")
        self.commit_all(nested)
        os.rename(nested, repo / "inner")
        self.assert_no_identity(review_capture.capture(str(repo), 30), "unsupported", "directory or nested repository in the path list")

    def test_real_fifo_in_place_of_a_tracked_file(self):
        repo = self.seeded_repo()
        (repo / "c.txt").unlink()
        os.mkfifo(repo / "c.txt")
        self.assert_no_identity(review_capture.capture(str(repo), 30), "unsupported", "path is neither a regular file nor a symlink")

    def test_untracked_fifo_is_not_in_the_domain(self):
        # The stated limit: Git never lists an untracked fifo, so it neither blocks nor enters the identity.
        repo = self.seeded_repo()
        before = self.ok(repo)
        os.mkfifo(repo / "pipe")
        self.assertEqual(self.ok(repo)["manifest_sha256"], before["manifest_sha256"])

    def test_real_untracked_file_created_between_the_two_listings(self):
        repo = self.seeded_repo()
        real = review_capture._git
        seen = {"others": 0}

        def hook(cwd, args, deadline):
            result = real(cwd, args, deadline)
            if "--others" in args:
                seen["others"] += 1
                if seen["others"] == 1:
                    (repo / "late.txt").write_text("late")
            return result
        with patched(_git=hook):
            result = review_capture.capture(str(repo), 30)
        self.assert_no_identity(result, "error", "path list changed during capture")
        self.assertEqual(seen["others"], 2)

    def move_between_listings(self, repo, prepare, move):
        """Run the capture with a hook that, after the first --others listing, moves a path between the listings."""
        prepare()
        real = review_capture._git
        seen = {"others": 0}

        def hook(cwd, args, deadline):
            result = real(cwd, args, deadline)
            if "--others" in args:
                seen["others"] += 1
                if seen["others"] == 1:
                    move()
            return result
        with patched(_git=hook):
            result = review_capture.capture(str(repo), 30)
        self.assertEqual(seen["others"], 2)
        return result

    def test_real_tracked_path_moves_from_the_cached_to_the_others_listing(self):
        # `git rm --cached` of a tracked file that stays on disk: the union of the listings does not change.
        repo = self.seeded_repo()
        result = self.move_between_listings(repo, lambda: None, lambda: self.git(repo, "rm", "-q", "--cached", "c.txt"))
        self.assertTrue((repo / "c.txt").exists())
        self.assertIn("c.txt", self.git(repo, "ls-files", "--others", "--exclude-standard"))
        self.assert_no_identity(result, "error", "path list changed during capture")

    def test_real_untracked_path_moves_from_the_others_to_the_cached_listing(self):
        # `git add` of an untracked file: the union of the listings does not change.
        repo = self.seeded_repo()
        result = self.move_between_listings(repo, lambda: (repo / "new.txt").write_text("n"),
                                            lambda: self.git(repo, "add", "new.txt"))
        self.assertIn("new.txt", self.git(repo, "ls-files", "--cached"))
        self.assert_no_identity(result, "error", "path list changed during capture")

    def test_real_tracked_file_deleted_and_removed_from_the_index_between_the_two_listings(self):
        repo = self.seeded_repo()
        real = review_capture._git
        seen = {"cached": 0}

        def hook(cwd, args, deadline):
            result = real(cwd, args, deadline)
            if "--cached" in args:
                seen["cached"] += 1
                if seen["cached"] == 1:
                    self.git(repo, "rm", "-q", "c.txt")
            return result
        with patched(_git=hook):
            self.assert_no_identity(review_capture.capture(str(repo), 30), "error", "path list changed during capture")

    def test_real_file_swapped_for_an_equal_size_file_during_the_open(self):
        # A is put back before the second lstat; only the descriptor shows that B was read.
        repo = self.seeded_repo()
        target = repo / "a.txt"
        other = self.base / "other.txt"
        other.write_text("TWO\n")
        saved = self.base / "saved.txt"
        real_open = review_capture._open

        def swap(path, flags):
            if os.fsdecode(path).endswith("/a.txt"):
                os.rename(target, saved)
                os.rename(other, target)
                fd = real_open(path, flags)
                os.rename(target, other)
                os.rename(saved, target)
                return fd
            return real_open(path, flags)
        with patched(_open=swap):
            result = review_capture.capture(str(repo), 30)
        self.assertEqual(target.read_text(), "one\n")
        self.assert_no_identity(result, "error", "changed during capture")

    def swap_at_open(self, repo, replace):
        """Capture with a hook that replaces a.txt between the first lstat and the open. A guard stops a blocking open."""
        real_open = review_capture._open
        seen = []

        def hook(path, flags):
            if os.fsdecode(path).endswith("/a.txt"):
                seen.append(flags)
                self.assertTrue(flags & os.O_NONBLOCK, "the open could block on a fifo")
                self.assertTrue(flags & os.O_NOFOLLOW, "the open would follow a symlink")
                replace()
            return real_open(path, flags)

        def too_slow(signum, frame):
            raise AssertionError("the open blocked")
        old = signal.signal(signal.SIGALRM, too_slow)
        signal.alarm(10)
        try:
            with patched(_open=hook):
                result = review_capture.capture(str(repo), 30)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        self.assertEqual(len(seen), 1)
        return result

    def test_real_fifo_put_in_place_of_a_file_between_the_first_lstat_and_the_open(self):
        repo = self.seeded_repo()
        target = repo / "a.txt"

        def replace():
            target.unlink()
            os.mkfifo(target)
        result = self.swap_at_open(repo, replace)
        self.assertTrue(stat.S_ISFIFO(os.lstat(target).st_mode))
        self.assert_no_identity(result, "error", "cannot read a file")

    def test_real_symlink_put_in_place_of_a_file_between_the_first_lstat_and_the_open(self):
        repo = self.seeded_repo()
        target = repo / "a.txt"

        def replace():
            target.unlink()
            os.symlink("c.txt", target)
        result = self.swap_at_open(repo, replace)
        self.assertTrue(target.is_symlink())
        self.assert_no_identity(result, "error", "cannot read a file")

    def test_real_file_changed_by_a_write_during_the_read(self):
        repo = self.seeded_repo()
        real = review_capture._read_file

        def writing(path, expected, check):
            if os.fsdecode(path).endswith("/a.txt"):
                with open(path, "ab") as handle:
                    handle.write(b"more")
            return real(path, expected, check)
        with patched(_read_file=writing):
            self.assert_no_identity(review_capture.capture(str(repo), 30), "error", "changed during capture")

    def test_temporary_folder_outside_git(self):
        plain = self.base / "plain"
        plain.mkdir()
        result = review_capture.capture(str(plain), 30)
        self.assert_no_identity(result, "not-git", "not inside a Git worktree")
        self.assertIsNone(result["root"])

    def test_the_two_listings_are_stable_in_a_quiet_repository(self):
        repo = self.seeded_repo()
        (repo / "new.txt").write_text("n")
        self.assertEqual(self.ok(repo)["entries"], 7)


def _raise(error):
    raise error


def snapshot(top):
    """Map every path under top, top itself included, to what ACC-4 compares.

    Directory: mode. Symlink: mode and target. Regular file: mode, size, mtime_ns and sha256 of the bytes.
    The path inventory is the set of keys. Nothing is skipped: a path that cannot be read raises, so that
    a cell fails instead of comparing less.
    """
    top = str(top)

    def record(path):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            return ("link", info.st_mode, os.readlink(path))
        if stat.S_ISDIR(info.st_mode):
            return ("dir", info.st_mode)
        if stat.S_ISREG(info.st_mode):
            with open(path, "rb") as handle:
                data = handle.read()
            return ("file", info.st_mode, info.st_size, info.st_mtime_ns, sha(data))
        raise AssertionError("neither a directory, a link nor a regular file: " + path)
    result = {".": record(top)}
    for folder, dirs, files in os.walk(top, followlinks=False, onerror=_raise):
        for name in dirs + files:
            path = os.path.join(folder, name)
            result[os.path.relpath(path, top)] = record(path)
    return result


class NoWriteTests(GitCase):
    """ACC-4: the capture and git_common_dir change nothing, over the matrix of the contract."""

    def make_stale(self, repo):
        # Same content with a new mtime: a command that refreshed the index would rewrite .git/index.
        (repo / "untracked.txt").write_text("u")
        future = os.stat(repo / "a.txt").st_mtime + 50
        os.utime(repo / "a.txt", (future, future))

    def test_main_worktree_is_unchanged(self):
        repo = self.seeded_repo()
        self.make_stale(repo)
        before = snapshot(repo)
        self.assertIn(os.path.join(".git", "index"), before)
        self.ok(repo)
        self.assertEqual(snapshot(repo), before)

    TRACE_FILES = ("trace.log", "event.json", "normal.txt", "perf.txt", "setup.txt")
    # setting -> (environment, global Trace2 configuration, trace files that plain Git writes with it)
    SETTINGS = {
        "none": (lambda f: {}, lambda f: {}, set()),
        "env": (lambda f: {"GIT_TRACE": f["trace.log"], "GIT_TRACE2_EVENT": f["event.json"], "GIT_TRACE2": f["normal.txt"],
                           "GIT_TRACE2_PERF": f["perf.txt"], "GIT_TRACE_SETUP": f["setup.txt"]},
                lambda f: {}, {"trace.log", "event.json", "normal.txt", "perf.txt", "setup.txt"}),
        "config": (lambda f: {},
                   lambda f: {"trace2.eventTarget": f["event.json"], "trace2.normalTarget": f["normal.txt"],
                              "trace2.perfTarget": f["perf.txt"]},
                   {"event.json", "normal.txt", "perf.txt"}),
        "both": (lambda f: {"GIT_TRACE": f["trace.log"], "GIT_TRACE2_EVENT": f["event.json"]},
                 lambda f: {"trace2.normalTarget": f["normal.txt"], "trace2.perfTarget": f["perf.txt"]},
                 {"trace.log", "event.json", "normal.txt", "perf.txt"}),
    }
    SETTING_NAMES = ("none", "env", "config", "both")

    cells_made = 0
    EXPECTED_STATE = {
        "ok": "ok", "unsupported": "unsupported", "timeout-zero-budget": "timeout", "timeout-injected-clock": "timeout",
        "error": "error", "error-injected-stat-change": "error", "error-injected-list-change": "error",
        "error-corrupt-index": "error", "not-git": "not-git",
    }

    EXPECTED_REASON = {
        "ok": None, "unsupported": "gitlink in the index", "timeout-zero-budget": "time budget used up",
        "timeout-injected-clock": "time budget used up", "error": "cannot read a file",
        "error-injected-stat-change": "changed during capture",
        "error-injected-list-change": "path list changed during capture", "error-corrupt-index": "git ls-files failed",
        "not-git": "not inside a Git worktree",
    }

    def run_capture(self, state, target):
        """Produce the state.
        Real: ok; unsupported (a gitlink in the index); timeout-zero-budget (seconds=0); error (a mode 000 tracked file,
        set up by cell(); as root _read_file is injected to raise instead); error-corrupt-index (a garbage index
        file, so that git ls-files fails); not-git (a plain folder).
        Injected: timeout-injected-clock (a fake clock that expires after the second listing),
        error-injected-stat-change (_lstat reports another mtime on its second call for a.txt) and
        error-injected-list-change (the second --others listing gains a path)."""
        if state in ("ok", "unsupported", "not-git", "error-corrupt-index"):
            return review_capture.capture(str(target), 30)
        if state == "timeout-zero-budget":
            return review_capture.capture(str(target), 0)
        if state == "timeout-injected-clock":
            now = [0.0]
            real = review_capture._git
            seen = {"others": 0}

            def hook(cwd, args, deadline):
                result = real(cwd, args, deadline)
                if "--others" in args:
                    seen["others"] += 1
                    if seen["others"] == 2:
                        now[0] = 100.0
                return result
            with patched(_git=hook, _clock=lambda: now[0]):
                return review_capture.capture(str(target), 5)
        if state == "error-injected-stat-change":
            calls = {}

            def shifting(path):
                info = os.lstat(path)
                if os.fsdecode(path).endswith("/a.txt"):
                    calls[path] = calls.get(path, 0) + 1
                    if calls[path] == 2:
                        return stat_like(info, st_mtime_ns=info.st_mtime_ns + 1)
                return info
            with patched(_lstat=shifting):
                return review_capture.capture(str(target), 30)
        if state == "error-injected-list-change":
            real = review_capture._git
            seen = {"others": 0}

            def late(cwd, args, deadline):
                code, out = real(cwd, args, deadline)
                if "--others" in args:
                    seen["others"] += 1
                    if seen["others"] == 2:
                        out += b"zzz-late.txt\0"  # the second listing names a path that is not on disk
                return code, out
            with patched(_git=late):
                return review_capture.capture(str(target), 30)
        assert state == "error", state
        if os.geteuid() == 0:
            def refuse(path, expected, check):
                raise PermissionError("injected: running as root")
            with patched(_read_file=refuse):
                return review_capture.capture(str(target), 30)
        return review_capture.capture(str(target), 30)

    def cell(self, layout, setting, state="ok"):
        """One cell of {main, linked} x {none, env, config, both} x state, or a plain folder x setting (not-git)."""
        type(self).cells_made += 1
        number = type(self).cells_made
        if layout == "plain":
            repo = target = self.base / ("plain%d" % number)
            repo.mkdir()
            (repo / "x.txt").write_text("x")
            common = None
        else:
            repo = self.seeded_repo("repo%d" % number)
            self.make_stale(repo)
            if layout == "linked":
                target = self.base / ("linked%d" % number)
                self.git(repo, "worktree", "add", "-q", str(target), "-b", "side%d" % number)
                (target / "extra.txt").write_text("e")
                future = os.stat(target / "c.txt").st_mtime + 50
                os.utime(target / "c.txt", (future, future))
                self.assertTrue(os.path.isfile(target / ".git"))
            else:
                target = repo
            common = repo / ".git"
            if state == "unsupported":
                head = self.git(repo, "rev-parse", "HEAD").strip()
                self.git(target, "update-index", "--add", "--cacheinfo", "160000," + head + ",subm")
            if state == "error-corrupt-index":
                index = self.git(target, "rev-parse", "--path-format=absolute", "--git-path", "index").strip()
                Path(index).write_bytes(b"not an index file\n")
        trace_dir = common if common is not None else self.base / ("traces%d" % number)
        trace_dir.mkdir(exist_ok=True)
        files = {key: str(trace_dir / key) for key in self.TRACE_FILES}
        if layout == "linked":
            files["setup.txt"] = str(common / "worktrees" / target.name / "setup.txt")  # the linked tree's own Git folder
        env, config, expected_written = self.SETTINGS[setting]
        global_config = str(self.base / ("trace-global%d.cfg" % number))
        Path(global_config).write_text("")
        for name, value in config(files).items():
            if common is not None:
                self.git(repo, "config", name, value)  # repository-local: Git itself ignores trace2.* here
            subprocess.run(["git", "config", "--file", global_config, name, value], check=True)
        full_env = dict(env(files), GIT_CONFIG_GLOBAL=global_config)
        if setting != "none":
            # Control: without the module, the same settings make plain git write exactly these trace files.
            with mock.patch.dict(os.environ, full_env):
                subprocess.run(["git", "-C", str(target), "ls-files"], capture_output=True, check=False)
            written = {key for key, path in files.items() if os.path.exists(path)}
            if layout == "plain":
                expected_written = expected_written - {"setup.txt"}  # Git prints no setup trace outside a repository
            self.assertEqual(written, expected_written, (layout, setting, state))
            for key in written:
                os.unlink(files[key])

        def snaps():
            result = {"tree": snapshot(target)}
            if common is not None:
                result["common git dir"] = snapshot(common)
                result["main tree"] = snapshot(repo)
            else:
                result["trace folder"] = snapshot(trace_dir)
            if layout == "linked":
                own = common / "worktrees" / target.name
                self.assertTrue(own.is_dir())
                result["linked git dir"] = snapshot(own)
            return result
        before = snaps()  # 1: before the capture. Every file is readable here.
        unreadable = None
        if state == "error" and os.geteuid() != 0:
            # Read permission is removed only while an operation runs, and restored before each snapshot.
            # So the bytes of a.txt are hashed in snapshots 1, 2 and 3, and compared each time.
            unreadable = target / "a.txt"
        original = stat.S_IMODE(os.stat(unreadable).st_mode) if unreadable else None

        def lock():
            if unreadable:
                os.chmod(unreadable, 0o000)

        def unlock():
            if unreadable:
                self.assertEqual(stat.S_IMODE(os.stat(unreadable).st_mode), 0)  # the operation did not chmod it
                os.chmod(unreadable, original)
        if unreadable:
            self.addCleanup(os.chmod, unreadable, original)
        no_trace = lambda: self.assertEqual([k for k, p in files.items() if os.path.exists(p)], [])  # noqa: E731
        lock()  # a.txt is unreadable during the capture
        with mock.patch.dict(os.environ, full_env):
            result = self.run_capture(state, target)
        unlock()
        self.assertEqual(result["state"], self.EXPECTED_STATE[state], (layout, setting, state, result))
        self.assertEqual(result["reason"], self.EXPECTED_REASON[state], (layout, setting, state, result))
        if state == "ok":
            self.assertEqual(result["root"], str(target))
        else:
            self.assertIsNone(result["manifest_sha256"])
        no_trace()
        after_capture = snaps()  # 2: after the capture, before git_common_dir. a.txt is readable again.
        lock()  # unreadable again during git_common_dir
        with mock.patch.dict(os.environ, full_env):
            found = review_capture.git_common_dir(str(target))
        unlock()
        self.assertEqual(found, None if common is None else str(common))
        no_trace()
        after_common = snaps()  # 3: after git_common_dir. a.txt is readable again.
        for name in before:
            self.assertEqual(after_capture[name], before[name], ("after the capture", layout, setting, state, name))
            self.assertEqual(after_common[name], before[name], ("after git_common_dir", layout, setting, state, name))

    def matrix(self, state, layouts=("main", "linked")):
        for layout in layouts:
            for setting in self.SETTING_NAMES:
                with self.subTest(layout=layout, setting=setting, state=state):
                    self.cell(layout, setting, state)

    def test_matrix_ok(self):
        self.matrix("ok")

    def test_matrix_unsupported_real_gitlink(self):
        self.matrix("unsupported")

    def test_matrix_timeout_with_a_zero_budget(self):
        self.matrix("timeout-zero-budget")

    def test_matrix_timeout_with_an_injected_clock_that_expires_after_the_listings(self):
        self.matrix("timeout-injected-clock")

    def test_matrix_error_real_unreadable_file(self):
        # A real mode 000 file when not root. As root, _read_file is injected to raise (see run_capture).
        self.matrix("error")

    def test_matrix_error_real_corrupt_index(self):
        self.matrix("error-corrupt-index")

    def test_matrix_error_with_an_injected_stat_change(self):
        self.matrix("error-injected-stat-change")

    def test_matrix_error_with_an_injected_change_of_the_second_listing(self):
        self.matrix("error-injected-list-change")

    def test_matrix_not_git_plain_folder_under_the_four_trace_settings(self):
        self.matrix("not-git", layouts=("plain",))

    def test_git_environment_has_no_trace_variable_and_trace_off(self):
        with mock.patch.dict(os.environ, {"GIT_TRACE_PACKET": "1", "GIT_TRACE_CURL": "1", "GIT_TRACE2_BRIEF": "1"}):
            env = review_capture._git_env()
        self.assertEqual({k: v for k, v in env.items() if k.startswith("GIT_TRACE")},
                         {"GIT_TRACE": "0", "GIT_TRACE2": "0", "GIT_TRACE2_PERF": "0", "GIT_TRACE2_EVENT": "0"})
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")

    def test_snapshot_detects_each_kind_of_change(self):
        repo = self.seeded_repo()
        os.symlink("a.txt", repo / "sl")
        self.assertEqual(snapshot(repo), snapshot(repo))  # control: no change, no difference

        def differs(label, change):
            before = snapshot(repo)  # a fresh baseline for each kind of change
            change()
            self.assertNotEqual(snapshot(repo), before, label)

        def longer():
            with open(repo / "a.txt", "ab") as handle:
                handle.write(b"more")

        def equal_length_with_restored_mtime():
            info = os.stat(repo / "c.txt")
            (repo / "c.txt").write_bytes(b"x" * info.st_size)
            os.utime(repo / "c.txt", ns=(info.st_atime_ns, info.st_mtime_ns))
            now = os.stat(repo / "c.txt")
            self.assertEqual((now.st_size, now.st_mtime_ns, now.st_mode), (info.st_size, info.st_mtime_ns, info.st_mode))

        def mode_change():
            before = os.stat(repo / "run.sh").st_mode
            os.chmod(repo / "run.sh", 0o600)
            self.assertNotEqual(os.stat(repo / "run.sh").st_mode, before)

        def new_file():
            (repo / "brand-new").write_text("n")

        def deleted_file():
            (repo / "tracked.log").unlink()

        def other_link_target():
            before = os.readlink(repo / "sl")
            os.unlink(repo / "sl")
            os.symlink("c.txt", repo / "sl")
            self.assertNotEqual(os.readlink(repo / "sl"), before)

        def new_file_in_git_dir():
            (repo / ".git" / "probe").write_text("p")

        def mode_change_of_a_directory():
            os.chmod(repo / ".git" / "refs", 0o700)

        for label, change in (("a write that changes the length", longer),
                              ("equal-length bytes with restored size, mtime and mode",
                               equal_length_with_restored_mtime),
                              ("a mode change", mode_change), ("a new file", new_file),
                              ("a deleted file", deleted_file), ("a changed symlink target", other_link_target),
                              ("a new file in the Git folder", new_file_in_git_dir),
                              ("a directory mode change", mode_change_of_a_directory)):
            with self.subTest(label):
                differs(label, change)

    def test_snapshot_fails_when_it_cannot_read_a_file_or_a_folder(self):
        repo = self.seeded_repo()
        with mock.patch("builtins.open", side_effect=PermissionError("injected")):
            with self.assertRaises(PermissionError):
                snapshot(repo)
        with mock.patch.object(os, "scandir", side_effect=PermissionError("injected")):
            with self.assertRaises(PermissionError):
                snapshot(repo)
        fifo_repo = self.seeded_repo("fifo")
        os.mkfifo(fifo_repo / "pipe")
        with self.assertRaises(AssertionError):
            snapshot(fifo_repo)


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, "-B", str(SCRIPT), *args], capture_output=True, text=True, check=False, cwd=cwd)


class CLITests(unittest.TestCase):
    """The command line and git_common_dir on this repository. Writes nothing."""

    def test_prints_one_json_object_with_sorted_keys_and_exits_0(self):
        result = run_cli("--seconds", "60", str(ROOT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("\n"), 1)
        data = json.loads(result.stdout)
        self.assertEqual(result.stdout, json.dumps(data, sort_keys=True) + "\n")
        self.assertEqual(set(data), {"manifest_version", "state", "reason", "manifest_sha256", "entries",
                                     "index_sha256", "root"})
        self.assertEqual(data["state"], "ok", data)
        self.assertEqual(data["manifest_sha256"], own_identity(ROOT)[0])

    def test_default_folder_is_the_current_folder(self):
        result = run_cli("--seconds", "60", cwd=str(ROOT / "checks"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["root"], os.fsdecode(git_out(ROOT, "rev-parse", "--show-toplevel")).strip())

    def test_exit_0_for_states_other_than_ok(self):
        result = run_cli("/")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "not-git")
        self.assertEqual(json.loads(result.stdout)["reason"], "not inside a Git worktree")
        result = run_cli("--seconds", "0", str(ROOT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "timeout")
        self.assertEqual(json.loads(result.stdout)["reason"], "time budget used up")
        self.assertIsNone(json.loads(result.stdout)["manifest_sha256"])
        for args in (["/"], ["--seconds", "0", str(ROOT)]):  # a non-ok run still prints one sorted JSON object
            out = run_cli(*args).stdout
            self.assertEqual(out.count("\n"), 1)
            self.assertEqual(out, json.dumps(json.loads(out), sort_keys=True) + "\n")
            self.assertEqual(len(json.loads(out)), 7)

    def test_help_prints_usage_text_and_exits_0(self):
        for flag in ("--help", "-h"):
            result = run_cli(flag)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith("usage: review_capture.py"), result.stdout)
            self.assertIn("--seconds", result.stdout)
            self.assertEqual(result.stderr, "")
            with self.assertRaises(ValueError):  # the usage text is not a capture result
                json.loads(result.stdout)

    def test_exit_2_on_a_usage_error(self):
        for args in (["--seconds", "abc"], ["--bogus"], ["--seconds", "-1"], ["--seconds", "nan"], ["--seconds", "inf"], ["a", "b"],
                     ["--seconds"]):
            result = run_cli(*args)
            self.assertEqual(result.returncode, 2, (args, result.stderr))
            self.assertEqual(result.stdout, "")

    def test_git_common_dir_of_this_repository(self):
        want = os.path.realpath(os.fsdecode(git_out(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")).strip())
        got = review_capture.git_common_dir(str(ROOT))
        self.assertEqual(got, want)
        self.assertTrue(os.path.isabs(got))
        self.assertEqual(review_capture.git_common_dir(ROOT / "checks"), want)

    def common_dir_with(self, answer):
        with mock.patch.object(review_capture.subprocess, "run", FakeRun({("common", 1): answer})):
            return review_capture.git_common_dir(str(FIXTURE))

    def test_git_common_dir_output_without_a_trailing_newline(self):
        want = os.path.realpath(str(FIXTURE / ".git"))
        self.assertEqual(self.common_dir_with((0, b".git")), want)
        self.assertEqual(self.common_dir_with((0, b".git\n")), want)
        self.assertEqual(self.common_dir_with((0, os.fsencode(str(FIXTURE / "elsewhere")))),
                         os.path.realpath(str(FIXTURE / "elsewhere")))  # an absolute answer stays as it is

    def test_git_common_dir_is_none_for_a_failure_with_output_and_for_empty_output(self):
        for label, answer in (("exit 128 with output", (128, b".git\n")), ("exit 1 with output, no newline", (1, b".git")),
                              ("exit 0 with empty output", (0, b"")), ("exit 0 with a newline only", (0, b"\n"))):
            with self.subTest(label):
                self.assertIsNone(self.common_dir_with(answer))

    def test_git_common_dir_is_none_when_git_cannot_start_or_is_too_slow(self):
        self.assertIsNone(self.common_dir_with(FileNotFoundError("git")))
        self.assertIsNone(self.common_dir_with(subprocess.TimeoutExpired("git", 5)))

    def test_git_common_dir_runs_the_exact_command_with_the_scrubbed_environment(self):
        run = FakeRun()
        with mock.patch.object(review_capture.subprocess, "run", run):
            review_capture.git_common_dir(str(FIXTURE))
        self.assertEqual(run.calls, [("common", str(FIXTURE), review_capture.DEFAULT_SECONDS)])

    def test_git_common_dir_outside_git(self):
        self.assertIsNone(review_capture.git_common_dir("/"))
        self.assertIsNone(review_capture.git_common_dir("/nonexistent-review-capture-folder"))


class RepoCLITests(GitCase):
    """The command line on temporary repositories. Needs a writable temporary folder."""

    def test_json_for_a_temporary_repository(self):
        repo = self.seeded_repo()
        result = run_cli("--seconds", "30", str(repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), review_capture.capture(str(repo), 30))
        self.assertEqual(json.loads(result.stdout)["manifest_sha256"], own_identity(repo)[0])

    def test_default_folder_in_a_temporary_repository(self):
        repo = self.seeded_repo()
        result = run_cli(cwd=str(repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["root"], str(repo))

    def test_exit_0_for_a_gitlink_and_a_temporary_non_git_folder(self):
        repo = self.seeded_repo()
        head = self.git(repo, "rev-parse", "HEAD").strip()
        self.git(repo, "update-index", "--add", "--cacheinfo", "160000," + head + ",sub")
        result = run_cli(str(repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "unsupported")
        self.assertEqual(json.loads(result.stdout)["reason"], "gitlink in the index")
        plain = self.base / "plain"
        plain.mkdir()
        result = run_cli(str(plain))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "not-git")
        self.assertEqual(json.loads(result.stdout)["reason"], "not inside a Git worktree")

    def test_git_common_dir_of_a_main_worktree(self):
        repo = self.seeded_repo()
        (repo / "sub").mkdir()
        expected = str(repo / ".git")
        self.assertEqual(review_capture.git_common_dir(str(repo)), expected)
        self.assertEqual(review_capture.git_common_dir(repo / "sub"), expected)

    def one_object(self, result, state, reason):
        self.assertEqual(result.returncode, 0, (state, result.stderr))
        self.assertEqual(result.stdout.count("\n"), 1, (state, result.stdout))
        data = json.loads(result.stdout)
        self.assertIsInstance(data, dict)
        self.assertEqual(result.stdout, json.dumps(data, sort_keys=True) + "\n")
        self.assertEqual(data["state"], state, data)
        self.assertEqual(data["reason"], reason, data)
        if state != "ok":
            self.assertIsNone(data["manifest_sha256"])
        return data

    def corrupt_index(self, repo):
        (repo / ".git" / "index").write_bytes(b"not an index file\n")

    def test_exit_0_for_state_error_from_a_failing_git_command(self):
        # A corrupt index makes `git ls-files -s -z` fail. This does not depend on file permissions or on the user.
        repo = self.seeded_repo()
        self.corrupt_index(repo)
        data = self.one_object(run_cli("--seconds", "30", str(repo)), "error", "git ls-files failed")
        self.assertEqual(data["reason"], "git ls-files failed")

    def test_every_state_prints_exactly_one_json_object_and_exits_0(self):
        repo = self.seeded_repo("ok-repo")
        self.one_object(run_cli("--seconds", "30", str(repo)), "ok", None)
        self.one_object(run_cli("--seconds", "0", str(repo)), "timeout", "time budget used up")
        plain = self.base / "plain"
        plain.mkdir()
        self.one_object(run_cli(str(plain)), "not-git", "not inside a Git worktree")
        gitlink = self.seeded_repo("gitlink-repo")
        head = self.git(gitlink, "rev-parse", "HEAD").strip()
        self.git(gitlink, "update-index", "--add", "--cacheinfo", "160000," + head + ",subm")
        self.one_object(run_cli(str(gitlink)), "unsupported", "gitlink in the index")
        broken = self.seeded_repo("broken-repo")
        self.corrupt_index(broken)
        self.one_object(run_cli(str(broken)), "error", "git ls-files failed")

    def test_command_line_on_a_linked_worktree(self):
        repo = self.seeded_repo()
        linked = self.base / "linked"
        self.git(repo, "worktree", "add", "-q", str(linked), "-b", "side")
        result = run_cli("--seconds", "30", str(linked))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["state"], "ok", data)
        self.assertEqual(data["root"], str(linked))
        self.assertEqual(data["manifest_sha256"], own_identity(linked)[0])

    def test_git_common_dir_of_a_linked_worktree(self):
        repo = self.seeded_repo()
        linked = self.base / "linked"
        self.git(repo, "worktree", "add", "-q", str(linked), "-b", "side")
        (linked / "deep").mkdir()
        self.assertEqual(review_capture.git_common_dir(linked / "deep"), str(repo / ".git"))
        self.assertEqual(review_capture.git_common_dir(str(linked)), str(repo / ".git"))
        self.assertTrue(os.path.isabs(review_capture.git_common_dir(str(linked))))
        self.assertEqual(review_capture.git_common_dir(str(linked)), review_capture.git_common_dir(str(repo)))

    def test_git_common_dir_outside_git(self):
        plain = self.base / "plain"
        plain.mkdir()
        self.assertIsNone(review_capture.git_common_dir(str(plain)))


if __name__ == "__main__":
    unittest.main()
