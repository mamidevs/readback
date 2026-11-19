"""The concurrency unit is a (repository, task, worktree) block in CURRENT.md."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import task_current as current
from project_registry import RegistryError


A = ("repo", "invoices", "/work/main")
B = ("repo", "shipping", "/work/main")
SHARED = b"CURRENT   legacy\r\nGOAL      ship\r\nOPEN      already tracked\r\nNEXT      finish legacy work"


def write(raw, key, token=None, opened="start", following="finish"):
    return current.merge(raw, *key, token, opened, following, "legacy").encode()


class CurrentTests(unittest.TestCase):
    def error(self, code, action, *args):
        with self.assertRaises(RegistryError) as caught:
            action(*args)
        self.assertEqual(caught.exception.code, code)

    def test_independent_initial_reads_merge_in_either_order(self):
        for first, second in ((A, B), (B, A)):
            with self.subTest(first=first):
                raw = write(SHARED, first)
                state = current.snapshot(raw, *first)
                new = write(raw, second, None, "pending", "next action")
                self.assertTrue(new.startswith(SHARED))
                self.assertEqual(current.snapshot(new, *first), state)
                self.assertEqual(current.snapshot(new, *second)["open"], "pending")
                self.assertEqual(current.shared_line_count(new.decode()), 4)

    def test_stale_same_block_and_nonexistent_digest_conflict(self):
        missing = {"expected_block_sha256": None, "open": "", "next": ""}
        self.assertEqual(current.snapshot(None, *A), missing)
        raw = write(None, A)
        self.assertTrue(raw.startswith(b"CURRENT   legacy\n"))
        token = current.snapshot(raw, *A)["expected_block_sha256"]
        self.error("CONFLICT", write, raw, A)
        self.error("CONFLICT", write, raw, B, token)
        changed = write(raw, A, token, "bring evidence", "close")
        self.error("CONFLICT", write, changed, A, token)
        self.assertEqual(current.snapshot(changed, *A)["next"], "close")

    def test_single_block_replacement_preserves_unrelated_bytes_and_digest(self):
        raw = write(write(SHARED, A), B)
        token = current.snapshot(raw, *A)["expected_block_sha256"]
        untouched = raw[raw.index(b'READBACK-TASK {"repository":"repo","task":"shipping"'):]
        raw = write(raw, A, token, "start\nretry", "do\nnext")
        self.assertTrue(raw.startswith(SHARED))
        self.assertTrue(raw.endswith(untouched))
        self.assertEqual(current.snapshot(raw, *A)["next"], "do\nnext")

    def test_append_after_missing_final_newline_keeps_the_prior_token(self):
        raw = write(None, A).rstrip(b"\n")
        before = current.snapshot(raw, *A)
        joined = write(raw, B)
        self.assertEqual(current.snapshot(joined, *A), before)
        edited = write(joined, A, before["expected_block_sha256"], "done", "review")
        self.assertEqual(current.snapshot(edited, *B)["open"], "start")

    def test_three_distinct_dimensions_of_identity_no_separator_collisions(self):
        raw = None
        keys = [A, B, ("another", A[1], A[2]), (A[0], A[1], "/work/alternate"),
                ("a|b", "c", "/d"), ("a", "b|c", "/d"), ("r", 'quo"te', "/w")]
        for n, key in enumerate(keys):
            raw = write(raw, key, opened=str(n))
        for n, key in enumerate(keys):
            self.assertEqual(current.snapshot(raw, *key)["open"], str(n))

    def test_values_do_not_inject_structure(self):
        fake = '\nEND-READBACK-TASK\nREADBACK-TASK {"repository":"new","task":"a","worktree":"b"}\n'
        raw = write(None, A, opened=fake, following='\nOPEN      "spurious"')
        self.assertEqual(current.snapshot(raw, *A)["open"], fake)
        self.assertEqual(current.snapshot(raw, "new", "a", "b")["expected_block_sha256"], None)
        self.assertEqual(len(raw.splitlines()), 5)
        for value in (42, "bad\rcommand", "has\0null", "a\u2028split"):
            self.error("INVALID", write, None, A, None, value)
        self.error("INVALID", write, None, ("r","a\nb","/w"))

    def test_malformed_duplicate_or_truncated_blocks_refuse_reads_and_writes(self):
        raw = write(None, A)
        block = raw[raw.index(b"READBACK-TASK "):]
        for corrupt in (block + block, block.rsplit(b"END-",1)[0], b"END-READBACK-TASK\n",
                        b"  READBACK-TASK {}\n", b"READBACK-TASK-broken\n",
                        block.replace(b'"task":"invoices"', b'"task":"other","task":"invoices"'),
                        block.replace(b'NEXT      "finish"',b'NEXT      12'),
                        block.replace(b'OPEN      "start"',b'OPEN no quotes')):
            self.error("INVALID", current.snapshot, corrupt, *B)
            self.error("INVALID", write, corrupt, B)

    def test_whole_document_writer_can_edit_only_shared_state(self):
        raw = write(SHARED, A)
        text = raw.decode()
        current.preserve_blocks(raw, text.replace("GOAL      ship", "GOAL      deliver"))
        for changed in [SHARED.decode(), text.replace('"finish"', '"steal"'), write(raw, B).decode()]:
            self.error("CONFLICT", current.preserve_blocks, raw, changed)
        self.error("CONFLICT", current.preserve_blocks, None, text)
        current.preserve_blocks(None, SHARED.decode())

    def test_more_than_thirty_document_lines_and_a_per_block_limit(self):
        raw = None
        for n in range(10):
            raw = write(raw, ("repo",f"task{n}","tree"))
        self.assertGreater(len(raw.splitlines()), 30)
        self.assertEqual(current.shared_line_count(raw.decode()), 1)
        self.error("INVALID", write, raw, B, None, "\n"*27)


if __name__ == "__main__":
    unittest.main()
