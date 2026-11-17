#!/usr/bin/env python3
"""Checks for the commit observation: scripts/commit_observation.py and its call from hooks/guard_git.py.

Run one class or several: python3 -B checks/test_commit_observation.py FormTests StoreTests

FormTests   the pure part, with bounded I/O guards (builtins.open, os.open, os.write, os.mkdir, subprocess.run).
StoreTests  the store part in this process, in fresh main and linked-worktree fixtures.
HookTests   the hook as a process, in the plugin layout and the portable layout, against the hook at the base commit.
FaultTests  injected faults in a hook process, against the hook at the base commit.
SyncTests   the portable copies.

The hook at the base commit comes from `git -C <repository> show 2929b899585092be2da505cd739d272336d919bb:hooks/guard_git.py`.
Both layouts are built in a temporary folder from hooks/ and scripts/. Only SyncTests reads portable/.
"""

import ast
import calendar
import concurrent.futures
import contextlib
import errno
import importlib.util
import json
import os
from pathlib import Path
import re
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
BASE_COMMIT = "2929b899585092be2da505cd739d272336d919bb"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "checks"))

_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True  # importing the modules below must not write __pycache__ folders
import commit_observation as co  # noqa: E402
import review_capture  # noqa: E402
from test_guard_git import ALLOWED, DENIED  # noqa: E402

sys.dont_write_bytecode = _bytecode


def load_module(name, path):
    """Load a module from a path without writing bytecode."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = before
    return module

TIME = "2026-11-17T01:02:03Z"
MANIFEST = "b" * 64
INDEX = "c" * 64
ATTEMPT = "a1" * 16
KEY = "e" * 64
LAYOUTS = ("plugin", "portable")
KINDS = ("main", "linked")
FILE_KEYS = ["capture", "commit", "commit_count", "elapsed_ms", "eligibility", "parse", "reviews", "reviews_state",
             "schema_version", "sub_agent", "target", "time", "unmatched_markers"]

# ----------------------------------------------------------------------------- the command lines

# Distinct texts. Each holds the letters a, F, C, c, t, u, S and m, so a consumed value that is read as flags shows.
VALUES = {"M": "aFCctuSm_SENT_MSG", "F": "aFCctuSm_SENT_FILE", "K": "aFCctuSm_SENT_KEY", "K2": "aFCctuSm_SENT_KEY2",
          "P1": "aFCctuSm_SENT_P1", "P2": "aFCctuSm_SENT_P2", "R": "aFCctuSm_SENT_REV"}
HOOK_VALUES = dict(VALUES, P1="p1", P2="p2")


# JSON-encoded commands of the contract; each is decoded before it goes to the walk or the hook.
JSON_C54 = json.loads(r'''"git commit -m \"$(cat <<'EOF'\n(\nEOF\n)\""''')
JSON_C55 = json.loads(r'''"git commit -m \"$(cat <<'EOF'\nit's\nEOF\n)\""''')
JSON_HEREDOC_PAREN = json.loads(r'''"git commit -m \"$(cat <<'EOF'\n1) first\nEOF\n)\""''')
JSON_HEREDOC_QUOTES = json.loads(r'''"git commit -m \"$(cat <<'EOF'\nsay \"hi\"\nEOF\n)\""''')
# the parse value of a row that is not `ok` when it holds a commit call and `None` when it holds none
PARSE = {"C40": "failed", "C53": "failed", "C54": "failed", "C55": "partial", "C56": "failed", "C58": "failed",
         "X-substitution-and-unbalanced-quote": "partial"}


def parse_expected(row):
    return PARSE.get(row[0], "ok" if row[2] else None)


def cm(everything=False, amend=False, dry=False, source="none", paths=0):
    return {"all": everything, "amend": amend, "dry_run": dry, "message_source": source, "pathspecs": paths}


MSG = cm(source="message")
# (id, template, commit_count, target, the -C values in order, the commit object of the first call)
CONTRACT_LINES = [
    ("C1", "git commit -m {M}", 1, "resolved", [], MSG),
    ("C2", "git commit -am {M}", 1, "resolved", [], cm(everything=True, source="message")),
    ("C3", "git commit -a -m {M}", 1, "resolved", [], cm(everything=True, source="message")),
    ("C4", "git commit --all --message={M}", 1, "resolved", [], cm(everything=True, source="message")),
    ("C5", "git commit --message {M}", 1, "resolved", [], MSG),
    ("C6", "git commit -m{M}", 1, "resolved", [], MSG),
    ("C7", "git commit --amend --no-edit", 1, "resolved", [], cm(amend=True)),
    ("C8", "git commit --amend -m {M}", 1, "resolved", [], cm(amend=True, source="message")),
    ("C9", "git commit -F {F}", 1, "resolved", [], cm(source="file")),
    ("C10", "git commit --file={F}", 1, "resolved", [], cm(source="file")),
    ("C11", "git commit -C {R}", 1, "resolved", [], cm(source="reuse")),
    ("C12", "git commit -c {R}", 1, "resolved", [], cm(source="reuse")),
    ("C13", "git commit --reuse-message={R}", 1, "resolved", [], cm(source="reuse")),
    ("C14", "git commit --reedit-message {R}", 1, "resolved", [], cm(source="reuse")),
    ("C15", "git commit --fixup {R}", 1, "resolved", [], cm(source="fixup")),
    ("C16", "git commit --squash {R}", 1, "resolved", [], cm(source="squash")),
    ("C17", "git commit -m {M} -- {P1} {P2}", 1, "resolved", [], cm(source="message", paths=2)),
    ("C18", "git commit {P1} -m {M}", 1, "resolved", [], cm(source="message", paths=1)),
    ("C19", "git commit -m {M} --author {K} --date {K2} {P1}", 1, "resolved", [], cm(source="message", paths=1)),
    ("C20", "git commit --dry-run", 1, "resolved", [], cm(dry=True)),
    ("C21", "git commit", 1, "resolved", [], cm()),
    ("C22", "git commit -S -m {M}", 1, "resolved", [], MSG),
    ("C23", "git commit -S{K} -m {M}", 1, "resolved", [], MSG),
    ("C24", "git commit -uno -m {M}", 1, "resolved", [], MSG),
    ("C25", "git commit -m {M} -m {M}", 1, "resolved", [], MSG),
    ("C26", "git commit -t {F}", 1, "resolved", [], cm()),
    ("C27", "git commit --pathspec-from-file={F} -m {M}", 1, "resolved", [], cm(source="message", paths=None)),
    ("C28", "git commit --trailer {K} --cleanup=strip -m {M}", 1, "resolved", [], MSG),
    ("C29", "git add -A && git commit -m {M}", 1, "resolved", [], MSG),
    ("C30", "git -C {P1} commit -m {M}", 1, "resolved", ["P1"], MSG),
    ("C31", "git -c user.name={K} commit -m {M}", 1, "resolved", [], MSG),
    ("C32", "cd {P1} && git commit -m {M}", 1, "unknown", [], MSG),
    ("C33", "git --git-dir={P1} commit -m {M}", 1, "unknown", [], MSG),
    ("C34", "GIT_DIR={P1} git commit -m {M}", 1, "unknown", [], MSG),
    ("C35", "env FOO=1 /usr/bin/git commit -m {M}", 1, "resolved", [], MSG),
    ("C36", "bash -c 'git commit -m {M}'", 1, "resolved", [], MSG),
    ("C37", "echo $(git commit -m {M})", 1, "resolved", [], MSG),
    ("C38", "git commit -m {M}; git commit --amend -m {M}", 2, "resolved", [], MSG),
    ("C39a", "git commit-graph write", 0, None, [], None),
    ("C39b", "git log --grep commit", 0, None, [], None),
    ("C39c", "git show commit", 0, None, [], None),
    ("C39d", "git status", 0, None, [], None),
    ("C40", "git commit -m '{M}", 0, None, [], None),
    ("C56", "git commit -m '{M}", 0, None, [], None),
    ("C41", "echo `git commit -m {M}`", 1, "resolved", [], MSG),
    ("C42", "eval git commit -m {M}", 1, "resolved", [], MSG),
    ("C43", "echo $(echo $(git commit -m {M}))", 1, "resolved", [], MSG),
    ("C44", "git -C {P1} -C {P2} commit -m {M}", 1, "resolved", ["P1", "P2"], MSG),
    ("C45a", "git --work-tree {P1} commit -m {M}", 1, "unknown", [], MSG),
    ("C45b", "git --namespace={K} commit -m {M}", 1, "unknown", [], MSG),
    ("C46a", "pushd {P1}; git commit -m {M}", 1, "unknown", [], MSG),
    ("C46b", "popd; git commit -m {M}", 1, "unknown", [], MSG),
    ("C47", "git commit -m {M} 2>&1", 1, "resolved", [], MSG),
    ("C48", "git commit -m {M} 2>{F}", 1, "resolved", [], MSG),
    ("C49", "git commit -m {M} >{F} 2>&1", 1, "resolved", [], MSG),
    ("C50", "git commit -m {M} {P1} 2>&1", 1, "resolved", [], cm(source="message", paths=1)),
    ("C51", "git commit -m 2 >{F}", 1, "resolved", [], MSG),
    ("C52", "git commit -m '2' >{F}", 1, "resolved", [], MSG),
    ("C53", "echo $(git commit -m ')')", 0, None, [], None),
    ("C54", JSON_C54, 0, None, [], None),
    ("C55", JSON_C55, 1, "resolved", [], MSG),
    ("C57a", "echo 'x", 0, None, [], None),
    ("C57b", "git status 'x", 0, None, [], None),
    ("C58", "git log --grep commit 'x", 0, None, [], None),
]

# Forms that the contract does not list. The names are for the coverage table.
EXTRA_LINES = [
    ("X-all-twice", "git commit -a -a", 1, "resolved", [], cm(everything=True)),
    ("X-consumed-dashdash", "git commit -m -- {P1}", 1, "resolved", [], cm(source="message", paths=1)),
    ("X-flag-after-consumed-dashdash", "git commit -m -- -a", 1, "resolved", [], cm(everything=True, source="message")),
    ("X-after-dashdash", "git commit -- -a", 1, "resolved", [], cm(paths=1)),
    ("X-second-dashdash", "git commit -- -- {P1}", 1, "resolved", [], cm(paths=2)),
    ("X-value-missing", "git commit -F", 1, "resolved", [], cm(source="file")),
    ("X-attached-C", "git commit -C{R}", 1, "resolved", [], cm(source="reuse")),
    ("X-verbose-m", "git commit -vm {M}", 1, "resolved", [], MSG),
    ("X-cluster-rest-then-word", "git commit -amF {P1}", 1, "resolved", [], cm(everything=True, source="message", paths=1)),
    ("X-cluster-value-next", "git commit -ac {R}", 1, "resolved", [], cm(everything=True, source="reuse")),
    ("X-author-equals", "git commit --author={K} {P1}", 1, "resolved", [], cm(paths=1)),
    ("X-author-next", "git commit --author {K}", 1, "resolved", [], cm()),
    ("X-u-takes-no-word", "git commit -u {P1}", 1, "resolved", [], cm(paths=1)),
    ("X-S-takes-no-word", "git commit -S {P1}", 1, "resolved", [], cm(paths=1)),
    ("X-single-dash", "git commit -", 1, "resolved", [], cm(paths=1)),
    ("X-unknown-long", "git commit --verbose {P1}", 1, "resolved", [], cm(paths=1)),
    ("X-fixup-then-m", "git commit --fixup {R} -m {M}", 1, "resolved", [], cm(source="fixup")),
    ("X-file-then-m", "git commit -F {F} -m {M}", 1, "resolved", [], cm(source="file")),
    ("X-reuse-then-m", "git commit -c {R} -m {M}", 1, "resolved", [], cm(source="reuse")),
    ("X-template-then-m", "git commit -t {F} -m {M}", 1, "resolved", [], MSG),
    ("X-squash-equals", "git commit --squash={R}", 1, "resolved", [], cm(source="squash")),
    ("X-pathspec-from-file-next", "git commit --pathspec-from-file {F} {P1}", 1, "resolved", [], cm(paths=None)),
    ("X-pathspec-from-file-after-dashdash", "git commit --pathspec-from-file={F} -- {P1}", 1, "resolved", [], cm(paths=None)),
    ("X-dry-run-amend-all", "git commit --dry-run --amend --all", 1, "resolved", [], cm(everything=True, amend=True, dry=True)),
    ("X-message-empty-equals", "git commit --message= {P1}", 1, "resolved", [], cm(source="message", paths=1)),
    ("X-message-last", "git commit --message", 1, "resolved", [], MSG),
    ("X-git-dir-next", "git --git-dir {P1} commit", 1, "unknown", [], cm()),
    ("X-git-dir-empty", "git --git-dir= commit", 1, "unknown", [], cm()),
    ("X-namespace-next", "git --namespace {K} commit", 1, "unknown", [], cm()),
    ("X-work-tree-equals", "git --work-tree={P1} commit", 1, "unknown", [], cm()),
    ("X-git-work-tree-env", "GIT_WORK_TREE={P1} git commit", 1, "unknown", [], cm()),
    ("X-git-env-after-env", "env GIT_INDEX_FILE={P1} git commit", 1, "unknown", [], cm()),
    ("X-git-word-without-equals", "GIT_FOO git commit", 1, "resolved", [], cm()),
    ("X-other-name-holding-git", "NOT_GIT_DIR=x git commit", 1, "resolved", [], cm()),
    ("X-git-after-the-call", "git commit GIT_DIR=x", 1, "resolved", [], cm(paths=1)),
    ("X-cd-after-the-call", "git commit -m {M}; cd {P1}", 1, "resolved", [], MSG),
    ("X-cdx-is-not-cd", "cdx {P1}; git commit", 1, "resolved", [], cm()),
    ("X-echo-cd", "echo cd; git commit", 1, "resolved", [], cm()),
    ("X-cd-in-a-shell", "bash -c 'cd {P1} && git commit -m {M}'", 1, "unknown", [], MSG),
    ("X-cd-in-a-group", "(cd {P1}; git commit -m {M})", 1, "unknown", [], MSG),
    ("X-cd-in-eval", "eval cd {P1}; git commit", 1, "unknown", [], cm()),
    ("X-cd-in-a-substitution", "echo $(cd {P1}); git commit", 1, "unknown", [], cm()),
    # Findings, pinned as the walk and D5 give them: the commit may run elsewhere, and the target is `resolved`.
    ("X-substitution-walked-before-the-cd", "cd {P1} && echo $(git commit -m {M})", 1, "resolved", [], MSG),
    ("X-git-env-before-a-shell", "GIT_DIR={P1} bash -c 'git commit -m {M}'", 1, "resolved", [], MSG),
    ("X-git-env-before-eval", "GIT_DIR={P1} eval git commit -m {M}", 1, "resolved", [], MSG),
    ("X-second-after-cd", "git commit -m {M} && cd {P1} && git commit --amend", 2, "resolved", [], MSG),
    ("X-global-flags", "git --no-pager -p commit", 1, "resolved", [], cm()),
    ("X-exec-path-next", "git --exec-path {P1} commit", 1, "resolved", [], cm()),
    ("X-config-env-equals", "git --config-env=a=b commit", 1, "resolved", [], cm()),
    ("X-c-and-C", "git -c a=b -C {P1} commit", 1, "resolved", ["P1"], cm()),
    ("X-C-takes-commit", "git -C commit", 0, None, [], None),
    ("X-C-last", "git -C", 0, None, [], None),
    ("X-c-last", "git -c", 0, None, [], None),
    ("X-git-alone", "git", 0, None, [], None),
    ("X-flag-last", "git -p", 0, None, [], None),
    ("X-git-last-word", "echo git", 0, None, [], None),
    ("X-C-equals", "git -C={P1} commit", 1, "resolved", ["P1"], cm()),
    ("X-C-empty", "git -C '' commit", 1, "resolved", [""], cm()),
    ("X-C-absolute", "git -C /abs/{P1} commit", 1, "resolved", ["/abs/{P1}"], cm()),
    ("X-path-git", "/usr/local/bin/git commit", 1, "resolved", [], cm()),
    ("X-relative-git", "./git commit", 1, "resolved", [], cm()),
    ("X-other-program", "mygit commit", 0, None, [], None),
    ("X-sudo", "sudo git commit", 1, "resolved", [], cm()),
    ("X-echo-git", "echo git commit", 1, "resolved", [], cm()),
    ("X-after-status", "git status commit", 0, None, [], None),
    ("X-second-git-word", "git log git commit", 1, "resolved", [], cm()),
    ("X-one-per-command", "git commit git commit", 1, "resolved", [], cm(paths=2)),
    ("X-pipe", "git commit -m {M} | cat", 1, "resolved", [], MSG),
    ("X-and", "git commit -m {M} && echo ok", 1, "resolved", [], MSG),
    ("X-or", "false || git commit -m {M}", 1, "resolved", [], MSG),
    ("X-group", "( git commit -m {M} )", 1, "resolved", [], MSG),
    ("X-redirect", "git commit -m {M} > out", 1, "resolved", [], MSG),
    ("X-heredoc-paren-in-a-message", JSON_HEREDOC_PAREN, 1, "resolved", [], MSG),
    ("X-heredoc-quotes-in-a-message", JSON_HEREDOC_QUOTES, 1, "resolved", [], MSG),
    ("X-fd-words-kept", "git commit -m {M} x2 y>F", 1, "resolved", [], cm(source="message", paths=2)),
    ("X-digit-before-a-pipe", "git commit -m {M} 2 | cat", 1, "resolved", [], cm(source="message", paths=1)),
    ("X-digit-before-input", "git commit -m {M} 2<{F}", 1, "resolved", [], MSG),
    ("X-digit-before-append", "git commit -m {M} 12>>{F}", 1, "resolved", [], MSG),
    ("X-digit-before-an-ampersand-redirect", "git commit -m {M} 2 &>{F}", 1, "resolved", [], cm(source="message", paths=1)),
    ("X-newline", "git status\ngit commit -m {M}", 1, "resolved", [], MSG),
    ("X-two-substitutions", "echo $(git commit -m {K}) $(git commit -a)", 2, "resolved", [], cm(source="message")),
    ("X-quoted-substitution", "echo \"$(git commit -m {M})\"", 1, "resolved", [], MSG),
    ("X-substitution-in-substitution", "echo $(a $(git commit -m {M}) b)", 1, "resolved", [], MSG),
    ("X-backtick-in-substitution", "echo $(echo `git commit -m {M}`)", 1, "resolved", [], MSG),
    ("X-substitution-in-backtick", "echo `echo $(git commit -m {M})`", 1, "resolved", [], MSG),
    ("X-unbalanced-substitution", "echo $(git commit -m {M}", 1, "resolved", [], MSG),
    ("X-substitution-and-unbalanced-quote", "echo $(git commit -m {M}) 'x", 1, "resolved", [], MSG),
    ("X-substitution-first-in-walk-order", "git commit -m {K}; echo $(git commit -a)", 2, "resolved", [], cm(everything=True)),
    ("X-substitution-value", "git commit -m $(date) {P1}", 1, "resolved", [], cm(source="message", paths=1)),
    ("X-eval-quoted", "eval \"git commit -m {M}\"", 1, "resolved", [], MSG),
    ("X-eval-after-assignment", "FOO=1 eval git commit -m {M}", 1, "resolved", [], MSG),
    ("X-eval-substitution", "eval echo $(git commit -m {M})", 1, "resolved", [], MSG),
    ("X-eval-only", "eval", 0, None, [], None),
    ("X-sh", "sh -c \"git commit -m {M}\"", 1, "resolved", [], MSG),
    ("X-zsh", "zsh -c 'git commit -m {M}'", 1, "resolved", [], MSG),
    ("X-dash", "dash -c 'git commit -m {M}'", 1, "resolved", [], MSG),
    ("X-ksh", "ksh -c 'git commit -m {M}'", 1, "resolved", [], MSG),
    ("X-bash-lc", "/bin/bash -lc 'git commit -m {M}'", 1, "resolved", [], MSG),
    ("X-bash-long-option", "bash --norc -c 'git commit -m {M}'", 1, "resolved", [], MSG),
    ("X-bash-without-c", "bash script.sh 'git commit'", 0, None, [], None),
    ("X-bash-c-last", "bash -c", 0, None, [], None),
    ("X-nested-shell", "bash -c \"sh -c 'git commit -m {M}'\"", 1, "resolved", [], MSG),
    ("X-shell-string-is-git", "bash -c git commit", 0, None, [], None),
    ("X-commit-in-an-argument", "echo commit git", 0, None, [], None),
]


def render(template, values):
    return template.format(**values)


def base_for(cwd, names, values):
    base = cwd
    for name in names:
        base = os.path.join(base, name.format(**values) if "{" in name else values.get(name, name))
    return base


# ------------------------------------------------------------------------------------- helpers

def remove(path):
    for folder, _dirs, _files in os.walk(path):
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)


def new_tmp(test):
    path = os.path.realpath(tempfile.mkdtemp(prefix="co-"))
    test.addCleanup(remove, path)
    return path


def clean_env(**extra):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") and key not in ("READBACK_COMMIT_CHECK", "PYTHONDONTWRITEBYTECODE")}
    env.update(extra)
    return env


def git(cwd, *args):
    env = clean_env(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "-C", cwd, *args],
        check=True, capture_output=True, env=env, timeout=30)


class Repo:
    """A fresh repository below `parent` with the folder readback/reviews. `kind` linked works in a linked worktree."""

    def __init__(self, parent, kind="main", name="repo"):
        self.kind = kind
        self.main = os.path.join(parent, name)
        os.makedirs(self.main)
        git(self.main, "init", "-q")
        Path(self.main, "tracked.txt").write_text("x\n")
        self.base = self.main
        if kind == "linked":
            git(self.main, "commit", "-q", "--allow-empty", "-m", "base")
            self.base = os.path.join(parent, name + "-linked")
            git(self.main, "worktree", "add", "-q", self.base, "-b", "linked")
            Path(self.base, "linked.txt").write_text("y\n")
        self.common = os.path.realpath(os.path.join(self.main, ".git"))
        self.readback = os.path.join(self.common, "readback")
        self.reviews = os.path.join(self.readback, "reviews")
        self.markers = os.path.join(self.readback, "by-manifest")
        self.file = os.path.join(self.readback, "commit-observations.jsonl")
        os.makedirs(self.reviews)

    def lines(self):
        if not os.path.exists(self.file):
            return []
        data = Path(self.file).read_bytes()
        return [json.loads(part) for part in data.split(b"\n") if part]

    def data(self):
        return Path(self.file).read_bytes()

    def tree(self):
        found = []
        for folder, dirs, files in os.walk(self.common):
            found.extend(os.path.join(folder, name) for name in dirs + files)
        return sorted(found)


def record(attempt=ATTEMPT, key=KEY, created="2026-11-16T10:00:00Z", verdict="PASS", binding="ends-equal",
           contract="c1" * 32, handoff="d4" * 32):
    return {"schema_version": 1, "attempt_id": attempt, "review_key": key, "created": created, "binding": binding,
            "contract": {"path": "/records/c.md", "sha256": contract},
            "handoff": {"path": "/records/h.md", "sha256": handoff},
            "judge": {"verdict": verdict, "provider": "codex"}, "validation": {"valid": True}}


def entry(attempt=ATTEMPT, key=KEY, created="2026-11-16T10:00:00Z", verdict="PASS", binding="ends-equal",
          contract="c1" * 32, handoff="d4" * 32):
    return {"attempt_id": attempt, "review_key": key, "created": created, "verdict": verdict, "binding": binding,
            "contract_sha256": contract, "handoff_sha256": handoff}


def put_record(repo, key, attempt, content=None):
    folder = os.path.join(repo.reviews, key)
    os.makedirs(folder, exist_ok=True)
    data = content if isinstance(content, bytes) else json.dumps(content if content is not None else record(attempt, key)).encode()
    Path(folder, attempt + ".completed.json").write_bytes(data)


def put_marker(repo, manifest, attempt):
    folder = os.path.join(repo.markers, manifest)
    os.makedirs(folder, exist_ok=True)
    Path(folder, attempt).write_bytes(b"")


def add_review(repo, attempt=ATTEMPT, key=KEY, manifest=MANIFEST, **fields):
    put_record(repo, key, attempt, record(attempt, key, **fields))
    put_marker(repo, manifest, attempt)
    return entry(attempt, key, **fields)


def expected_line(**override):
    line = {
        "schema_version": 2, "parse": "ok", "time": TIME, "sub_agent": False, "commit_count": 1, "commit": dict(MSG),
        "target": "resolved", "capture": {"state": "ok", "reason": None, "manifest_sha256": MANIFEST, "index_sha256": INDEX},
        "reviews": [], "reviews_state": "ok", "unmatched_markers": 0, "elapsed_ms": 0, "eligibility": "UNVERIFIED",
    }
    line.update(override)
    return line


def line_bytes(line):
    return (json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def ok_result(manifest=MANIFEST, index=INDEX, **override):
    result = {"manifest_version": 1, "state": "ok", "reason": None, "manifest_sha256": manifest, "entries": 1,
              "index_sha256": index, "root": "/root"}
    result.update(override)
    return result


class Clock:
    """A controlled monotonic clock. After the last scripted value it repeats the last one."""

    def __init__(self, values, log=None):
        self.values = list(values)
        self.calls = 0
        self.log = log

    def __call__(self):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        if self.log is not None:
            self.log.append("clock")
        return value


class Bounded(unittest.TestCase):
    """Each test has a deadline, so that a loop that does not end fails the test instead of the run."""

    deadline = 60

    def setUp(self):
        def expired(_signal, _frame):
            raise TimeoutError("the test did not end in %d seconds" % self.deadline)

        previous = signal.signal(signal.SIGALRM, expired)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        self.addCleanup(signal.alarm, 0)
        signal.alarm(self.deadline)


# --------------------------------------------------------------------------- process helpers

def run_process(command, event=None, env=None, cwd=None, raw=None):
    """Run a command with the event as JSON on stdin. The result holds bytes: no text mode."""
    data = raw if raw is not None else json.dumps(event).encode("utf-8")
    return subprocess.run(command, input=data, capture_output=True, env=env if env is not None else clean_env(),
                          cwd=cwd, timeout=30)


def differences(first, second):
    """The names of the parts in which two results differ: stdout, stderr and the exit status, compared as bytes."""
    assert isinstance(first.stdout, bytes) and isinstance(second.stdout, bytes)
    assert isinstance(first.stderr, bytes) and isinstance(second.stderr, bytes)
    found = []
    if first.stdout != second.stdout:
        found.append("stdout")
    if first.stderr != second.stderr:
        found.append("stderr")
    if first.returncode != second.returncode:
        found.append("returncode")
    return found


class Result:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def bash_event(command, cwd=None, **extra):
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        event["cwd"] = cwd
    event.update(extra)
    return event


MODULES = ("commit_observation", "review_capture")


def build_layout(kind, dest, replace=None, drop=(), extra=None):
    """Build the plugin or the portable layout below dest from hooks/ and scripts/. Return (hook path, module folder)."""
    replace = replace or {}
    hook_dir = Path(dest, "hooks") if kind == "plugin" else Path(dest)
    module_dir = Path(dest, "scripts") if kind == "plugin" else Path(dest)
    hook_dir.mkdir(parents=True, exist_ok=True)
    module_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "hooks" / "guard_git.py", hook_dir / "guard_git.py")
    for name in MODULES:
        if name in drop:
            continue
        if name in replace:
            (module_dir / (name + ".py")).write_bytes(replace[name])
        else:
            shutil.copyfile(ROOT / "scripts" / (name + ".py"), module_dir / (name + ".py"))
    for relative, data in (extra or {}).items():
        target = Path(dest, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return str(hook_dir / "guard_git.py"), str(module_dir)


def base_hook(folder):
    """The hook at the base commit: `git show <base>:hooks/guard_git.py`, written below folder."""
    shown = subprocess.run(["git", "-C", str(ROOT), "show", BASE_COMMIT + ":hooks/guard_git.py"],
                           capture_output=True, check=True, timeout=30)
    path = Path(folder, "oracle", "guard_git.py")
    path.parent.mkdir(parents=True)
    path.write_bytes(shown.stdout)
    return str(path)


class Oracle:
    """Runs the hook at the base commit once for each event and keeps the result."""

    def __init__(self, path):
        self.path = path
        self.cache = {}

    def result(self, event=None, raw=None, env=None, cwd=None):
        key = (raw if raw is not None else json.dumps(event, sort_keys=True), tuple(sorted((env or {}).items())), cwd)
        if key not in self.cache:
            self.cache[key] = run_process([sys.executable, self.path], event, env=env, cwd=cwd, raw=raw)
        return self.cache[key]


def pooled(function, items):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(function, items))


TRACE_COMMIT_OBSERVATION = (
    b'import os, sys\n'
    b'open(os.environ["TRACE"] + ".path", "w").write(repr(sys.path))\n'
    b'open(os.environ["TRACE"], "a").write("commit_observation " + os.path.dirname(os.path.abspath(__file__)) + "\\n")\n'
    b'import review_capture\n'
    b'def observe(commands, whole, command, event):\n'
    b'    open(os.environ["TRACE"], "a").write("observe " + repr(commands) + "\\n")\n'
)
TRACE_REVIEW_CAPTURE = (
    b'import os\n'
    b'open(os.environ["TRACE"], "a").write("review_capture " + os.path.dirname(os.path.abspath(__file__)) + "\\n")\n'
)


def commit_repo(test, kind="main", name="repo"):
    """A repository for a hook run: readback/reviews, and the folders p1 and p1/p2 for -C."""
    tmp = new_tmp(test)
    repo = Repo(tmp, kind, name)
    os.makedirs(os.path.join(repo.base, "p1", "p2"))
    return repo


# ===================================================================================== FormTests

class FormTests(Bounded):
    """The pure part of the module and the walk of the hook, with the bounded I/O guards of E6 active."""

    deadline = 5

    @classmethod
    def setUpClass(cls):
        cls.hook = load_module("guard_git_under_test", ROOT / "hooks" / "guard_git.py")

    def setUp(self):
        super().setUp()
        for target in ("builtins.open", "os.open", "os.write", "os.mkdir", "subprocess.run"):
            patcher = mock.patch(target, side_effect=AssertionError("I/O in the pure part: " + target))
            patcher.start()
            self.addCleanup(patcher.stop)

    def find(self, line):
        commands, whole = self.hook.simple_commands(line)
        return commands, co.find_commit(commands), whole

    def selected(self, commands, whole, line):
        """The decision of the hook for the import, written as main() writes it."""
        return bool(self.hook.holds_commit(commands) or (not whole and self.hook.CANDIDATE.search(line)))

    def check_row(self, row, values, cwd):
        row_id, template, count, target, c_names, commit = row
        line = render(template, values)
        commands, (found, call), whole = self.find(line)
        self.assertEqual(found, count, row_id)
        self.assertEqual(self.hook.holds_commit(commands), count > 0, row_id)
        self.assertEqual(co.parse_of(found, whole, line), parse_expected(row), row_id)
        self.assertEqual(self.selected(commands, whole, line), parse_expected(row) is not None, row_id)
        if count == 0:
            self.assertIsNone(call, row_id)
            return
        self.assertIsNotNone(call, row_id)
        self.assertEqual(co.target_of(commands, call), target, row_id)
        expected_base = cwd
        for name in c_names:
            expected_base = os.path.join(expected_base, name.format(**values) if "{" in name else values.get(name, name))
        self.assertEqual(co.base_folder(cwd, call), expected_base, row_id)
        self.assertEqual(co.commit_object(call["args"]), commit, row_id)

    def test_the_guards_raise(self):
        for call in (lambda: open("x"), lambda: os.open("x", 0), lambda: os.write(1, b"x"), lambda: os.mkdir("x"),
                     lambda: subprocess.run(["true"])):
            with self.assertRaises(AssertionError):
                call()

    def test_commands_c1_to_c46(self):
        for row in CONTRACT_LINES:
            with self.subTest(row=row[0]):
                self.check_row(row, VALUES, "/work/dir")

    def test_c1_without_a_cwd_has_the_base_dot(self):
        commands, (count, call), _whole = self.find("git commit -m " + VALUES["M"])
        self.assertEqual(count, 1)
        for event in ({}, {"cwd": None}, {"cwd": 5}, {"cwd": ["/x"]}, {"agent_type": "a"}):
            with self.subTest(event=event):
                self.assertEqual(co.cwd_of(event), ".")
                self.assertEqual(co.base_folder(co.cwd_of(event), call), ".")
        self.assertEqual(co.cwd_of({"cwd": "/work/dir"}), "/work/dir")
        self.assertEqual(co.cwd_of({"cwd": ""}), "")
        _, (_, with_c), _whole = self.find("git -C " + VALUES["P1"] + " commit")
        self.assertEqual(co.base_folder(co.cwd_of({}), with_c), os.path.join(".", VALUES["P1"]))

    def test_extra_forms(self):
        for row in EXTRA_LINES:
            with self.subTest(row=row[0]):
                self.check_row(row, VALUES, "/work/dir")

    def test_the_hook_predicate_and_the_module_agree(self):
        for row in CONTRACT_LINES + EXTRA_LINES:
            line = render(row[1], VALUES)
            commands, whole = self.hook.simple_commands(line)
            with self.subTest(row=row[0]):
                self.assertEqual(self.hook.holds_commit(commands), co.find_commit(commands)[0] > 0)
                self.assertEqual(self.selected(commands, whole, line),
                                 co.parse_of(co.find_commit(commands)[0], whole, line) is not None)
        self.assertFalse(self.hook.holds_commit([]))
        self.assertFalse(self.hook.holds_commit([[]]))

    def test_the_candidate_pattern_is_the_same_in_the_hook_and_the_module(self):
        self.assertEqual(self.hook.CANDIDATE.pattern, co.CANDIDATE.pattern)
        self.assertEqual(co.CANDIDATE.pattern, r"\bgit\b[^;&|\n]*?\bcommit\b")
        for text, expected in (("git commit", True), ("x git -C y commit", True), ("git; commit", False),
                               ("git\ncommit", False), ("mygit commit", False), ("git commits", False),
                               ("git log --grep commit 'x", True), ("echo 'x", False)):
            self.assertEqual(bool(co.CANDIDATE.search(text)), expected, text)
            self.assertEqual(bool(self.hook.CANDIDATE.search(text)), expected, text)

    def test_the_parse_value(self):
        parse = co.parse_of
        self.assertEqual(parse(1, True, "git commit"), "ok")
        self.assertEqual(parse(2, True, "x"), "ok")
        self.assertEqual(parse(1, False, "git commit"), "partial")
        self.assertEqual(parse(3, False, "x"), "partial")
        self.assertEqual(parse(0, False, "git commit -m 'x"), "failed")
        self.assertEqual(parse(0, False, "git log --grep commit 'x"), "failed")
        self.assertIsNone(parse(0, False, "echo 'x"))
        self.assertIsNone(parse(0, False, "git status 'x"))
        self.assertIsNone(parse(0, True, "git commit"))
        self.assertIsNone(parse(0, True, "git status"))

    def test_the_judge_case_and_the_heredoc_forms(self):
        cases = (
            ("echo $(git commit -m ')')", [], False, 0, "failed"),
            (JSON_C54, [], False, 0, "failed"),
            (JSON_C55, [["git", "commit", "-m", "$()"]], False, 1, "partial"),
            (JSON_HEREDOC_PAREN, [["cat"], ["EOF"], ["1"], ["git", "commit", "-m", "$() first ; EOF ; )"]], True, 1, "ok"),
            (JSON_HEREDOC_QUOTES, [["cat"], ["EOF"], ["say", "hi"], ["EOF"], ["git", "commit", "-m", "$()"]], True, 1,
             "ok"),
        )
        for line, words, whole, count, parse in cases:
            with self.subTest(line=line):
                commands, complete = self.hook.simple_commands(line)
                self.assertEqual((commands, complete), (words, whole))
                self.assertEqual(co.find_commit(commands)[0], count)
                self.assertEqual(self.hook.holds_commit(commands), count > 0)
                self.assertEqual(co.parse_of(count, complete, line), parse)
                self.assertEqual(self.selected(commands, complete, line), True)
        self.assertEqual(JSON_C55, "git commit -m \"$(cat <<'EOF'\nit's\nEOF\n)\"")
        self.assertEqual(JSON_HEREDOC_QUOTES, "git commit -m \"$(cat <<'EOF'\nsay \"hi\"\nEOF\n)\"")
        commit = co.commit_object(co.find_commit(self.hook.simple_commands(JSON_C55)[0])[1]["args"])
        self.assertEqual(commit, MSG)

    def test_a_file_descriptor_before_a_redirection_is_not_a_word(self):
        walk = lambda text: self.hook.simple_commands(text)  # noqa: E731
        self.assertEqual(walk("git commit -m M 2>&1"), ([["git", "commit", "-m", "M"], ["1"]], True))
        self.assertEqual(walk("a 2>F b"), ([["a"], ["F", "b"]], True))
        self.assertEqual(walk("a 12>F"), ([["a"], ["F"]], True))
        self.assertEqual(walk("2>F"), ([["F"]], True))
        self.assertEqual(walk("a 2 <F"), ([["a"], ["F"]], True))
        self.assertEqual(walk("a 2 >>F"), ([["a"], ["F"]], True))
        self.assertEqual(walk("a '2' >F"), ([["a"], ["F"]], True))
        self.assertEqual(walk("a 2 b"), ([["a", "2", "b"]], True))
        self.assertEqual(walk("a x2>F"), ([["a", "x2"], ["F"]], True))
        self.assertEqual(walk("a \u0662>F"), ([["a", "\u0662"], ["F"]], True))
        self.assertEqual(walk("a 2 | b"), ([["a", "2"], ["b"]], True))
        self.assertEqual(walk("a 2 ; b"), ([["a", "2"], ["b"]], True))
        self.assertEqual(walk("a 2 &>F"), ([["a", "2"], ["F"]], True))
        self.assertEqual(walk("a 2 && b"), ([["a", "2"], ["b"]], True))
        self.assertEqual(walk("a 2 2>F"), ([["a", "2"], ["F"]], True))
        self.assertEqual(walk("eval a 2>F"), ([["eval"], ["a"], ["F"]], True))
        self.assertEqual(walk("bash -c 'a 2>F' 2>G"), ([["bash", "-c"], ["a"], ["F"], ["G"]], True))
        self.assertEqual(walk("echo $(a 2>F)"), ([["a"], ["F"], ["echo", "$"]], True))

    def test_the_walk_reports_whether_every_text_was_split(self):
        walk = lambda text: self.hook.simple_commands(text)[1]  # noqa: E731
        for text in ("", "a", "a b; c", "echo $(a b)", "echo `a`", "eval a b", "bash -c 'a b'", "echo $(a $(b))"):
            self.assertIs(walk(text), True, text)
        for text in ("echo 'x", 'echo "x', "echo $(a 'x) b", "echo `a 'x`", "eval \"a 'x\"", "bash -c \"a 'x\"",
                     "echo $(echo $(a 'x))", "a $(b) 'x", "echo $(a) `b 'x`", "bash -c \"eval a 'x\""):
            self.assertIs(walk(text), False, text)

    def test_the_option_sets_are_the_same(self):
        self.assertEqual(co.GIT_OPTIONS_WITH_VALUE, self.hook.GIT_OPTIONS_WITH_VALUE)
        self.assertEqual(self.hook.SHELLS, {"bash", "sh", "zsh", "dash", "ksh"})

    def test_the_walk_returns_word_lists_in_walk_order(self):
        walk = lambda text: self.hook.simple_commands(text)[0]  # noqa: E731
        self.assertEqual(walk("git commit -m x"), [["git", "commit", "-m", "x"]])
        self.assertEqual(walk("a b; c d && e | f"), [["a", "b"], ["c", "d"], ["e"], ["f"]])
        self.assertEqual(walk(""), [])
        self.assertEqual(walk(";;"), [])
        self.assertEqual(walk("a\nb"), [["a"], ["b"]])
        self.assertEqual(walk("(a b)"), [["a", "b"]])
        self.assertEqual(walk("echo $(x y) z"), [["x", "y"], ["echo", "$", "z"]])
        self.assertEqual(walk("echo `x y` z"), [["x", "y"], ["echo", "z"]])
        self.assertEqual(walk("echo $(x $(y))"), [["y"], ["x", "$"], ["echo", "$"]])
        self.assertEqual(walk("echo $(x `y`)"), [["y"], ["x"], ["echo", "$"]])
        self.assertEqual(walk("echo `x $(y)`"), [["y"], ["x", "$"], ["echo"]])
        self.assertEqual(walk("$(a) $(b) `c` `d`"), [["a"], ["b"], ["c"], ["d"], ["$", "$"]])
        self.assertEqual(walk("$(a)$(b)"), [["a"], ["b"], ["$", "$"]])
        self.assertEqual(walk("echo $(a"), [["a"], ["echo", "$"]])
        self.assertEqual(walk("eval a b; c"), [["eval"], ["a", "b"], ["c"]])
        self.assertEqual(walk("x eval a b"), [["x", "eval"], ["a", "b"]])
        self.assertEqual(walk("bash -c 'a b' c"), [["bash", "-c", "c"], ["a", "b"]])
        self.assertEqual(walk("bash -lc 'a b'"), [["bash", "-lc"], ["a", "b"]])
        self.assertEqual(walk("bash --login -c 'a b'"), [["bash", "--login", "-c"], ["a", "b"]])
        self.assertEqual(walk("bash 'a b'"), [["bash", "a b"]])
        self.assertEqual(walk("bash -c"), [["bash", "-c"]])
        self.assertEqual(walk("zsh -x"), [["zsh", "-x"]])
        self.assertEqual(walk("echo 'a"), [])
        self.assertEqual(walk("echo $(a) 'b"), [["a"]])
        self.assertEqual(walk("a `b"), [["a", "b"]])
        # the body of a substitution ends before its closing parenthesis, and the scan counts nested ones
        self.assertEqual(walk("echo $(a b \\)"), [["echo", "$"]])
        self.assertEqual(walk("echo $((a) b)"), [["a", "b"], ["echo", "$"]])
        self.assertEqual(walk("echo $((a)) b"), [["a"], ["echo", "$", "b"]])
        self.assertEqual(walk("echo $(a) b)"), [["a"], ["echo", "$", "b"]])
        self.assertEqual(walk("a ;|&<> b"), [["a"], ["b"]])
        self.assertEqual(walk("a ; b"), [["a"], ["b"]])

    def test_a_text_that_shlex_cannot_split_gives_no_simple_command(self):
        self.assertEqual(self.hook.simple_commands("git commit -m 'unbalanced"), ([], False))
        self.assertEqual(self.hook.simple_commands('git commit -m "unbalanced'), ([], False))

    def test_the_commit_object_values_and_flags(self):
        commit = co.commit_object
        self.assertEqual(commit([]), cm())
        self.assertEqual(commit(["-a"]), cm(everything=True))
        self.assertEqual(commit(["--all"]), cm(everything=True))
        self.assertEqual(commit(["-xa"]), cm(everything=True))
        self.assertEqual(commit(["-m", "-a"]), cm(source="message"))
        self.assertEqual(commit(["-m-a"]), cm(source="message"))
        self.assertEqual(commit(["--message", "--amend"]), cm(source="message"))
        self.assertEqual(commit(["--message=--amend"]), cm(source="message"))
        self.assertEqual(commit(["--author", "--dry-run"]), cm())
        self.assertEqual(commit(["-S", "-a"]), cm(everything=True))
        self.assertEqual(commit(["-Sa"]), cm())
        self.assertEqual(commit(["-ua"]), cm())
        self.assertEqual(commit(["-u", "-a"]), cm(everything=True))
        self.assertEqual(commit(["-t", "-a"]), cm())
        self.assertEqual(commit(["-ta"]), cm())
        self.assertEqual(commit(["-C", "-a"]), cm(source="reuse"))
        self.assertEqual(commit(["-F", "x", "y"]), cm(source="file", paths=1))
        self.assertEqual(commit(["-m", "x", "--", "y", "z"]), cm(source="message", paths=2))
        self.assertEqual(commit(["--amend"]), cm(amend=True))
        self.assertEqual(commit(["--dry-run"]), cm(dry=True))
        self.assertEqual(commit(["--amend=x", "--dry-run=x", "--all=x"]), cm())
        self.assertEqual(commit(["--fixup=x"]), cm(source="fixup"))
        self.assertEqual(commit(["--squash", "x"]), cm(source="squash"))
        self.assertEqual(commit(["--reuse-message", "x"]), cm(source="reuse"))
        self.assertEqual(commit(["--reedit-message=x"]), cm(source="reuse"))
        for name in ("--template", "--cleanup", "--trailer", "--author", "--date"):
            self.assertEqual(commit([name, "x", "y"]), cm(paths=1), name)
            self.assertEqual(commit([name + "=x", "y"]), cm(paths=1), name)
        self.assertEqual(commit(["--pathspec-from-file", "x", "y"]), cm(paths=None))
        self.assertEqual(commit(["y", "--pathspec-from-file=x"]), cm(paths=None))

    def test_the_target_and_the_base_folder_of_a_built_call(self):
        commands = [["cd", "x"], ["GIT_DIR=a", "git", "-C", "p", "-c", "k=v", "-C", "q", "commit", "-m", "m"]]
        count, call = co.find_commit(commands)
        self.assertEqual(count, 1)
        self.assertEqual(call["position"], 1)
        self.assertEqual(call["before"], ["GIT_DIR=a"])
        self.assertEqual(call["options"], [("-C", "p"), ("-c", "k=v"), ("-C", "q")])
        self.assertEqual(call["args"], ["-m", "m"])
        self.assertEqual(co.target_of(commands, call), "unknown")
        self.assertEqual(co.target_of(commands[1:], dict(call, position=0)), "unknown")
        self.assertEqual(co.target_of([["git", "commit"]], dict(call, before=[], options=[], position=0)), "resolved")
        self.assertEqual(co.base_folder("/w", call), "/w/p/q")
        self.assertEqual(co.base_folder("/w", dict(call, options=[("-c", "k=v")])), "/w")
        self.assertEqual(co.base_folder("/w", dict(call, options=[("-C", "/abs")])), "/abs")
        self.assertEqual(co.find_commit([]), (0, None))
        for words in (["git"], ["git", "-C"], ["git", "--git-dir"], ["git", "-p"], ["x", "git", "-c"]):
            self.assertEqual(co.find_commit([words]), (0, None), words)
            self.assertFalse(self.hook.holds_commit([words]), words)
        self.assertEqual(co.find_commit([[], ["git"]]), (0, None))

    def test_the_line_object_has_exactly_the_keys_of_the_schema(self):
        line = co.line_object("ok", TIME, True, 2, MSG, "resolved", {"state": "ok"}, [], "ok", 0, 17)
        self.assertEqual(sorted(line), FILE_KEYS)
        self.assertEqual(line, {
            "schema_version": 2, "parse": "ok", "time": TIME, "sub_agent": True, "commit_count": 2, "commit": MSG,
            "target": "resolved", "capture": {"state": "ok"}, "reviews": [], "reviews_state": "ok",
            "unmatched_markers": 0, "elapsed_ms": 17, "eligibility": "UNVERIFIED"})
        self.assertEqual(co.capture_object("a", "b", "c", "d"),
                         {"state": "a", "reason": "b", "manifest_sha256": "c", "index_sha256": "d"})

    def test_the_agent_type_decides_sub_agent(self):
        for value, expected in (("readback:worker", True), ("x", True), ("", False), (None, False), (5, False),
                                (True, False), (["x"], False)):
            with self.subTest(value=value):
                self.assertIs(co.sub_agent_of({"agent_type": value}), expected)
        self.assertIs(co.sub_agent_of({}), False)

    def test_the_serialization(self):
        line = {"b": 1, "a": {"y": [1, 2], "x": "é\n\u2028\udcff"}, "c": None, "d": True}
        data = co.serialize(line)
        self.assertIsInstance(data, bytes)
        self.assertEqual(data, b'{"a":{"x":"\\u00e9\\n\\u2028\\udcff","y":[1,2]},"b":1,"c":null,"d":true}\n')
        data.decode("ascii")
        self.assertEqual(data.count(b"\n"), 1)
        self.assertEqual(json.loads(data), line)

    def test_the_constants_of_the_contract(self):
        self.assertEqual(co.SCHEMA_VERSION, 1)
        self.assertEqual(co.LINE_SCHEMA_VERSION, 2)
        self.assertEqual(co.BUDGET_SECONDS, 5)
        self.assertEqual(co.TARGET_OPTIONS, ("--git-dir", "--work-tree", "--namespace"))
        self.assertEqual(co.DIRECTORY_COMMANDS, ("cd", "pushd", "popd"))
        self.assertEqual(co.COMPLETED_SUFFIX, ".completed.json")
        for name in ("a" * 32, "0123456789abcdef" * 2, "f" * 32):
            self.assertTrue(co.MARKER_NAME.fullmatch(name), name)
        for name in ("a" * 31, "a" * 33, "A" * 32, "g" * 32, "a" * 32 + "\n", "\n" + "a" * 32, "a" * 31 + "é", ""):
            self.assertFalse(co.MARKER_NAME.fullmatch(name), repr(name))

    def test_the_forms_read_no_file_and_start_no_process(self):
        # The guards of setUp stay active for every test of this class; this one runs the whole pure part at once.
        commands = self.hook.simple_commands("git -C a commit -am b; echo $(git commit)")[0]
        count, call = co.find_commit(commands)
        co.line_object("ok", TIME, False, count, co.commit_object(call["args"]), co.target_of(commands, call), None, None,
                       "no-identity", None, 0)
        co.serialize({"a": 1})


# ===================================================================================== StoreTests

class StoreTests(Bounded):
    """The store part in this process, in fresh main and linked-worktree fixtures."""

    deadline = 90

    def setUp(self):
        super().setUp()
        self.tmp = new_tmp(self)
        self.count = 0

    def repos(self):
        """A fresh main and a fresh linked fixture, each in its own subtest."""
        for kind in KINDS:
            self.count += 1
            with self.subTest(kind=kind):
                yield Repo(self.tmp, kind, "repo%d" % self.count)

    def go(self, repo, words=None, event=None, clock=None, capture=None, now=TIME, patches=(), real_capture=False,
           whole=True, command="git commit -m x"):
        words = words if words is not None else [["git", "commit", "-m", "x"]]
        event = {"cwd": repo.base} if event is None else event
        clock = clock if clock is not None else Clock([100.0])
        calls = []

        def fake(path, seconds):
            calls.append((path, seconds))
            if isinstance(capture, BaseException):
                raise capture
            if callable(capture):
                return capture(path, seconds)
            return ok_result()

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(co, "_clock", clock))
            stack.enter_context(mock.patch.object(co, "_now", lambda: now))
            if not real_capture:
                stack.enter_context(mock.patch.object(review_capture, "capture", fake))
            for patch in patches:
                stack.enter_context(patch)
            path = co.observe(words, whole, command, event)
        outcome = type("Outcome", (), {})()
        outcome.path, outcome.clock, outcome.captures = path, clock, calls
        return outcome

    # ------------------------------------------------------------------ the line

    def test_a_line_with_every_field_and_the_path(self):
        for repo in self.repos():
            reviews = [add_review(repo)]
            outcome = self.go(repo, clock=Clock([100.0, 100.0, 100.0, 100.0, 100.0, 100.25]))
            self.assertEqual(outcome.path, repo.file)
            self.assertEqual(repo.file, os.path.join(repo.common, "readback", "commit-observations.jsonl"))
            self.assertEqual(repo.data(), line_bytes(expected_line(reviews=reviews, elapsed_ms=250)))
            self.assertEqual(outcome.captures, [(repo.base, 5.0)])
            self.assertEqual(outcome.clock.calls, 6)
            self.assertEqual(sorted(repo.lines()[0]), FILE_KEYS)
            if repo.kind == "linked":
                self.assertFalse(os.path.exists(os.path.join(repo.base, ".git", "readback")))

    def test_the_commit_object_and_the_count_enter_the_line(self):
        for repo in self.repos():
            words = [["git", "commit", "-am", "x", "--amend", "--", "p", "q"], ["git", "commit", "--dry-run"]]
            self.go(repo, words=words, event={"cwd": repo.base, "agent_type": "readback:worker"})
            line = repo.lines()[0]
            self.assertEqual(line["commit"], cm(everything=True, amend=True, source="message", paths=2))
            self.assertEqual(line["commit_count"], 2)
            self.assertIs(line["sub_agent"], True)
            self.assertEqual(line["target"], "resolved")
            self.assertEqual(line["schema_version"], 2)
            self.assertEqual(line["parse"], "ok")
            self.assertEqual(line["eligibility"], "UNVERIFIED")

    def test_a_line_with_parse_partial(self):
        for repo in self.repos():
            reviews = [add_review(repo)]
            outcome = self.go(repo, whole=False, command="git commit -m 'x' $(echo 'y)")
            self.assertEqual(repo.data(), line_bytes(expected_line(parse="partial", reviews=reviews)))
            self.assertEqual(outcome.captures, [(repo.base, 5.0)])
            self.assertEqual(repo.lines()[0]["schema_version"], 2)

    def test_a_line_with_parse_failed(self):
        for repo in self.repos():
            add_review(repo)
            clock = Clock([100.0, 100.5])
            outcome = self.go(repo, words=[], whole=False, command="git commit -m 'x", clock=clock)
            self.assertEqual(outcome.path, repo.file)
            self.assertEqual(outcome.captures, [])
            self.assertEqual(clock.calls, 2)
            self.assertEqual(repo.data(), line_bytes(expected_line(
                parse="failed", commit_count=None, commit=None, target="unknown", capture=None, reviews=None,
                reviews_state="no-identity", unmatched_markers=None, elapsed_ms=500)))

    def test_a_failed_line_goes_to_the_repository_of_the_cwd_and_of_nothing_else(self):
        for repo in self.repos():
            os.makedirs(os.path.join(repo.base, "sub"))
            # the commit call is not recognized, so no -C is read: the event's cwd is the storage base
            outcome = self.go(repo, words=[["git", "-C", "sub", "log"]], whole=False,
                              command="git -C sub commit -m 'x", clock=Clock([100.0]))
            self.assertEqual(outcome.captures, [])
            self.assertEqual(repo.lines()[0]["parse"], "failed")
            self.assertEqual(outcome.path, repo.file)
            sub_event = {"cwd": os.path.join(repo.base, "sub")}
            self.go(repo, words=[], whole=False, command="git commit -m 'x", event=sub_event)
            self.assertEqual(len(repo.lines()), 2)
            elsewhere = os.path.join(self.tmp, "elsewhere-" + repo.kind)
            os.makedirs(elsewhere)
            self.assertIsNone(self.go(repo, words=[], whole=False, command="git commit -m 'x",
                                      event={"cwd": elsewhere}).path)
            self.assertEqual(len(repo.lines()), 2)

    def test_a_failed_line_without_a_cwd_uses_dot(self):
        for repo in self.repos():
            previous = os.getcwd()
            os.chdir(repo.base)
            self.addCleanup(os.chdir, previous)
            outcome = self.go(repo, words=[], whole=False, command="git commit -m 'x", event={})
            os.chdir(previous)
            self.assertEqual(outcome.path, repo.file)
            self.assertEqual(repo.lines()[0]["parse"], "failed")
            self.assertEqual(outcome.captures, [])

    def test_a_failed_line_has_the_sub_agent_flag_and_no_unmatched_gate_bypass(self):
        for repo in self.repos():
            shutil.rmtree(repo.reviews)
            before = repo.tree()
            self.assertIsNone(self.go(repo, words=[], whole=False, command="git commit -m 'x").path)
            self.assertEqual(repo.tree(), before)
            os.makedirs(repo.reviews)
            self.go(repo, words=[], whole=False, command="git commit -m 'x",
                    event={"cwd": repo.base, "agent_type": "readback:worker"})
            self.assertIs(repo.lines()[0]["sub_agent"], True)

    def test_no_line_without_a_candidate(self):
        for repo in self.repos():
            clock = Clock([100.0])
            for words, whole, command in (([], False, "echo 'x"), ([["git", "status"]], False, "git status 'x"),
                                          ([], True, "git commit"), ([["git", "status"]], True, "git status"),
                                          ([], False, "git; commit 'x")):
                outcome = self.go(repo, words=words, whole=whole, command=command, clock=clock)
                self.assertIsNone(outcome.path, command)
            self.assertEqual(clock.calls, 0)
            self.assertFalse(os.path.exists(repo.file))
            outcome = self.go(repo, words=[], whole=False, command="git log --grep commit 'x")
            self.assertEqual(repo.lines()[0]["parse"], "failed")
            self.assertEqual(outcome.captures, [])

    def test_a_recognized_call_wins_over_the_pattern(self):
        for repo in self.repos():
            self.go(repo, words=[["git", "commit"]], whole=False, command="echo")
            self.assertEqual(repo.lines()[0]["parse"], "partial")
            self.assertEqual(repo.lines()[0]["commit_count"], 1)

    def test_sub_agent_true_and_false(self):
        for repo in self.repos():
            for agent, expected in (("readback:worker", True), (None, False), ("", False), (5, False)):
                self.go(repo, event={"cwd": repo.base, "agent_type": agent})
                self.assertIs(repo.lines()[-1]["sub_agent"], expected, agent)
            self.go(repo, event={"cwd": repo.base})
            self.assertIs(repo.lines()[-1]["sub_agent"], False)
            self.assertEqual(len(repo.lines()), 5)

    def test_the_time_is_utc_with_a_z(self):
        for repo in self.repos():
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(review_capture, "capture", lambda path, seconds: ok_result()))
                before = time.time()
                co.observe([["git", "commit"]], True, "git commit", {"cwd": repo.base})
                after = time.time()
            moment = repo.lines()[0]["time"]
            self.assertRegex(moment, r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
            parsed = calendar.timegm(time.strptime(moment, "%Y-%m-%dT%H:%M:%SZ"))
            self.assertGreaterEqual(parsed, int(before) - 1)
            self.assertLessEqual(parsed, after + 1)

    def test_the_real_capture_enters_the_line(self):
        for repo in self.repos():
            direct = review_capture.capture(repo.base, 5)
            self.assertEqual(direct["state"], "ok")
            self.go(repo, real_capture=True)
            line = repo.lines()[0]
            self.assertEqual(line["capture"], {"state": "ok", "reason": None, "manifest_sha256": direct["manifest_sha256"],
                                               "index_sha256": direct["index_sha256"]})
            self.assertEqual(line["reviews"], [])
            self.assertEqual(line["reviews_state"], "ok")
            self.assertEqual(line["unmatched_markers"], 0)

    def test_the_base_folder_names_the_capture_and_the_repository(self):
        for repo in self.repos():
            os.makedirs(os.path.join(repo.base, "sub", "deeper"))
            outcome = self.go(repo, words=[["git", "-C", "sub", "-C", "deeper", "commit"]])
            self.assertEqual(outcome.captures, [(os.path.join(repo.base, "sub", "deeper"), 5.0)])
            self.assertEqual(outcome.path, repo.file)

    def test_an_event_without_cwd_uses_dot(self):
        for repo in self.repos():
            previous = os.getcwd()
            os.chdir(repo.base)
            self.addCleanup(os.chdir, previous)
            outcome = self.go(repo, event={})
            os.chdir(previous)
            self.assertEqual(outcome.captures, [(".", 5.0)])
            self.assertEqual(outcome.path, repo.file)
            self.assertEqual(len(repo.lines()), 1)

    def test_an_unknown_target_takes_no_capture(self):
        for repo in self.repos():
            outcome = self.go(repo, words=[["cd", "x"], ["git", "commit", "-m", "x"]], clock=Clock([100.0, 105.0]))
            self.assertEqual(outcome.captures, [])
            self.assertEqual(outcome.clock.calls, 2)
            self.assertEqual(repo.data(), line_bytes(expected_line(
                target="unknown", capture=None, reviews=None, reviews_state="no-identity", unmatched_markers=None,
                elapsed_ms=5000)))
            self.assertEqual(outcome.path, repo.file)

    def test_a_command_without_a_commit_call_writes_nothing(self):
        for repo in self.repos():
            clock = Clock([100.0])
            outcome = self.go(repo, words=[["git", "status"], []], clock=clock)
            self.assertIsNone(outcome.path)
            self.assertEqual(outcome.captures, [])
            self.assertEqual(clock.calls, 0)
            self.assertFalse(os.path.exists(repo.file))
            self.assertIsNone(self.go(repo, words=[]).path)

    # ------------------------------------------------------------------ the capture states

    def test_a_capture_that_raises(self):
        for repo in self.repos():
            self.go(repo, capture=RuntimeError("injected"))
            self.assertEqual(repo.data(), line_bytes(expected_line(
                capture={"state": "error", "reason": "capture raised", "manifest_sha256": None, "index_sha256": None},
                reviews=None, reviews_state="no-identity", unmatched_markers=None)))

    def test_a_capture_without_the_keys_counts_as_raised(self):
        for repo in self.repos():
            self.go(repo, capture=lambda path, seconds: {"state": "ok"})
            self.assertEqual(repo.lines()[0]["capture"]["reason"], "capture raised")
            self.assertEqual(repo.lines()[0]["capture"]["state"], "error")

    def test_a_capture_that_is_not_ok(self):
        for repo in self.repos():
            for state, reason in (("unsupported", "gitlink in the index"), ("timeout", "time budget used up"),
                                  ("error", "cannot read a file"), ("not-git", "not inside a Git worktree")):
                add_review(repo)
                self.go(repo, capture=lambda path, seconds: ok_result(state=state, reason=reason, manifest_sha256=None))
                self.assertEqual(repo.lines()[-1], expected_line(
                    capture={"state": state, "reason": reason, "manifest_sha256": None, "index_sha256": INDEX},
                    reviews=None, reviews_state="no-identity", unmatched_markers=None))

    def test_a_keyboard_interrupt_is_not_caught(self):
        for repo in self.repos():
            with self.assertRaises(KeyboardInterrupt):
                self.go(repo, capture=KeyboardInterrupt())
            self.assertFalse(os.path.exists(repo.file))
            add_review(repo)
            with self.assertRaises(KeyboardInterrupt):
                self.go(repo, patches=[mock.patch.object(co, "_lstat", side_effect=KeyboardInterrupt())])
            self.assertFalse(os.path.exists(repo.file))

    # ------------------------------------------------------------------ the gate

    def test_no_line_and_no_new_folder_without_readback_reviews(self):
        for repo in self.repos():
            shutil.rmtree(repo.reviews)
            before = repo.tree()
            outcome = self.go(repo)
            self.assertIsNone(outcome.path)
            self.assertEqual(outcome.captures, [])
            self.assertEqual(repo.tree(), before)
            shutil.rmtree(repo.readback)
            before = repo.tree()
            self.assertIsNone(self.go(repo).path)
            self.assertEqual(repo.tree(), before)
            self.assertFalse(os.path.exists(repo.readback))

    def test_no_line_and_no_new_folder_when_reviews_is_a_file(self):
        for repo in self.repos():
            shutil.rmtree(repo.reviews)
            Path(repo.reviews).write_bytes(b"")
            before = repo.tree()
            self.assertIsNone(self.go(repo).path)
            self.assertEqual(repo.tree(), before)

    def test_no_line_when_the_common_directory_cannot_be_resolved(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        before = sorted(os.listdir(self.tmp))
        outcome = self.go(type("R", (), {"base": outside})(), event={"cwd": outside})
        self.assertIsNone(outcome.path)
        self.assertEqual(outcome.captures, [])
        self.assertEqual(sorted(os.listdir(self.tmp)), before)
        self.assertEqual(os.listdir(outside), [])
        for repo in self.repos():
            before = repo.tree()
            patch = mock.patch.object(review_capture, "git_common_dir", return_value=None)
            outcome = self.go(repo, patches=[patch])
            self.assertIsNone(outcome.path)
            self.assertEqual(repo.tree(), before)
            gone = os.path.join(self.tmp, "gone")
            self.assertIsNone(self.go(repo, event={"cwd": gone}).path)
            self.assertFalse(os.path.exists(gone))

    # ------------------------------------------------------------------ the reviews

    def test_an_absent_marker_folder(self):
        for repo in self.repos():
            outcome = self.go(repo, clock=Clock([100.0]))
            self.assertEqual(repo.lines()[0]["reviews"], [])
            self.assertEqual(repo.lines()[0]["reviews_state"], "ok")
            self.assertEqual(repo.lines()[0]["unmatched_markers"], 0)
            self.assertEqual(outcome.clock.calls, 4)

    def test_no_marker(self):
        for repo in self.repos():
            os.makedirs(os.path.join(repo.markers, MANIFEST))
            outcome = self.go(repo)
            self.assertEqual((repo.lines()[0]["reviews"], repo.lines()[0]["reviews_state"],
                              repo.lines()[0]["unmatched_markers"]), ([], "ok", 0))
            self.assertEqual(outcome.clock.calls, 4)

    def test_no_marker_needs_no_listing_of_the_reviews_folder(self):
        for repo in self.repos():
            os.makedirs(os.path.join(repo.markers, MANIFEST))
            real = os.listdir

            def failing(path):
                if str(path) == repo.reviews:
                    raise PermissionError(errno.EACCES, "injected")
                return real(path)

            self.go(repo, patches=[mock.patch.object(co, "_listdir", failing)])
            line = repo.lines()[0]
            self.assertEqual((line["reviews"], line["reviews_state"], line["unmatched_markers"]), ([], "ok", 0))

    def test_one_marker(self):
        for repo in self.repos():
            reviews = [add_review(repo)]
            self.go(repo, clock=Clock([100.0]))
            self.assertEqual((repo.lines()[0]["reviews"], repo.lines()[0]["reviews_state"],
                              repo.lines()[0]["unmatched_markers"]), (reviews, "ok", 0))

    def test_markers_of_another_manifest_are_not_read(self):
        for repo in self.repos():
            add_review(repo, manifest="9" * 64)
            self.go(repo)
            self.assertEqual((repo.lines()[0]["reviews"], repo.lines()[0]["unmatched_markers"]), ([], 0))

    def test_three_markers_of_two_contracts_sorted(self):
        for repo in self.repos():
            a = "a" * 32
            b = "b" * 32
            c = "c" * 32
            key1, key2 = "1" * 64, "2" * 64
            third = add_review(repo, c, key1, created=None, contract="aa" * 32, handoff="bb" * 32)
            first = add_review(repo, b, key2, created="2026-11-16T09:00:00Z", contract="cc" * 32, handoff="dd" * 32)
            second = add_review(repo, a, key1, created="2026-11-16T10:00:00Z", contract="aa" * 32, handoff="ee" * 32,
                                verdict="FAIL", binding="unbound: changed")
            self.go(repo)
            line = repo.lines()[0]
            self.assertEqual(line["reviews"], [third, first, second])
            self.assertEqual([item["attempt_id"] for item in line["reviews"]], [c, b, a])
            self.assertEqual(line["unmatched_markers"], 0)
            self.assertEqual(line["reviews_state"], "ok")
            self.assertEqual(second["verdict"], "FAIL")
            self.assertIsNone(third["created"])

    def test_the_same_attempt_in_two_keys_gives_two_entries_by_key(self):
        for repo in self.repos():
            attempt = "3" * 32
            put_record(repo, "kb", attempt, record(attempt, "kb", created="2026-02-02T00:00:00Z"))
            put_record(repo, "ka", attempt, record(attempt, "ka", created="2026-02-02T00:00:00Z"))
            put_marker(repo, MANIFEST, attempt)
            self.go(repo, clock=Clock([100.0]))
            line = repo.lines()[0]
            self.assertEqual([item["review_key"] for item in line["reviews"]], ["ka", "kb"])
            self.assertEqual(line["unmatched_markers"], 0)

    def test_created_then_attempt_then_review_key_with_distinct_values(self):
        for repo in self.repos():
            first, second, third = "1" * 32, "2" * 32, "3" * 32
            put_record(repo, "kz", third, record(third, "kz", created="2026-01-01T00:00:00Z"))
            put_record(repo, "ky", second, record(second, "ky", created="2026-01-01T00:00:00Z"))
            put_record(repo, "kx", first, record(first, "kx", created="2026-01-03T00:00:00Z"))
            put_record(repo, "kw", first, record(first, "kw", created="2026-01-03T00:00:00Z"))
            for attempt in (first, second, third):
                put_marker(repo, MANIFEST, attempt)
            self.go(repo)
            order = [(item["created"], item["attempt_id"], item["review_key"]) for item in repo.lines()[0]["reviews"]]
            self.assertEqual(order, [("2026-01-01T00:00:00Z", second, "ky"), ("2026-01-01T00:00:00Z", third, "kz"),
                                     ("2026-01-03T00:00:00Z", first, "kw"), ("2026-01-03T00:00:00Z", first, "kx")])

    def test_a_marker_without_a_valid_record_is_counted(self):
        for repo in self.repos():
            matched = add_review(repo, "1" * 32)
            put_marker(repo, MANIFEST, "2" * 32)
            put_marker(repo, MANIFEST, "3" * 32)
            put_record(repo, KEY, "3" * 32, b"not json")
            os.makedirs(os.path.join(repo.reviews, "empty"))
            self.go(repo)
            line = repo.lines()[0]
            self.assertEqual((line["reviews"], line["unmatched_markers"], line["reviews_state"]), ([matched], 2, "ok"))

    def test_each_malformed_record_is_invalid(self):
        valid = record(ATTEMPT, KEY)
        cases = {
            "empty": b"",
            "truncated": b'{"schema_version": 1',
            "not json": b"not json",
            "a list": json.dumps([valid]).encode(),
            "null": b"null",
            "a string": b'"x"',
            "a number": b"1",
            "no schema_version": json.dumps({k: v for k, v in valid.items() if k != "schema_version"}).encode(),
            "schema_version 2": json.dumps(dict(valid, schema_version=2)).encode(),
            "schema_version 0": json.dumps(dict(valid, schema_version=0)).encode(),
            "schema_version true": json.dumps(dict(valid, schema_version=True)).encode(),
            "schema_version 1.0": b'{"schema_version": 1.0, "attempt_id": "%s"}' % ATTEMPT.encode(),
            "schema_version 1.5": json.dumps(dict(valid, schema_version=1.5)).encode(),
            "schema_version text": json.dumps(dict(valid, schema_version="1")).encode(),
            "schema_version null": json.dumps(dict(valid, schema_version=None)).encode(),
            "no attempt_id": json.dumps({k: v for k, v in valid.items() if k != "attempt_id"}).encode(),
            "another attempt_id": json.dumps(dict(valid, attempt_id="f" * 32)).encode(),
            "attempt_id null": json.dumps(dict(valid, attempt_id=None)).encode(),
            "invalid utf-8": b'{"schema_version": 1, "attempt_id": "\xff\xfe"}',
            "too deep": b"[" * 100000,
        }
        for repo in self.repos():
            for name, content in cases.items():
                with self.subTest(case=name):
                    shutil.rmtree(repo.markers, ignore_errors=True)
                    shutil.rmtree(repo.reviews)
                    os.makedirs(repo.reviews)
                    put_marker(repo, MANIFEST, ATTEMPT)
                    put_record(repo, KEY, ATTEMPT, content)
                    self.go(repo)
                    line = repo.lines()[-1]
                    self.assertEqual((line["reviews"], line["reviews_state"], line["unmatched_markers"]), ([], "ok", 1))
            with self.subTest(case="valid control"):
                shutil.rmtree(repo.markers)
                shutil.rmtree(repo.reviews)
                os.makedirs(repo.reviews)
                put_marker(repo, MANIFEST, ATTEMPT)
                put_record(repo, KEY, ATTEMPT, valid)
                self.go(repo)
                self.assertEqual(repo.lines()[-1]["reviews"], [entry()])

    def test_an_entry_has_null_for_a_value_that_is_not_a_string_or_a_container_that_is_not_an_object(self):
        weird = [
            ("created number", {"created": 5}, {"created": None}),
            ("created null", {"created": None}, {"created": None}),
            ("binding list", {"binding": ["x"]}, {"binding": None}),
            ("judge missing", {"judge": None}, {"verdict": None}),
            ("judge list", {"judge": ["PASS"]}, {"verdict": None}),
            ("judge string", {"judge": "PASS"}, {"verdict": None}),
            ("verdict number", {"judge": {"verdict": 1}}, {"verdict": None}),
            ("contract list", {"contract": ["x"]}, {"contract_sha256": None}),
            ("contract sha number", {"contract": {"sha256": 7}}, {"contract_sha256": None}),
            ("contract sha missing", {"contract": {}}, {"contract_sha256": None}),
            ("handoff list", {"handoff": [1]}, {"handoff_sha256": None}),
            ("handoff sha bool", {"handoff": {"sha256": True}}, {"handoff_sha256": None}),
        ]
        for repo in self.repos():
            for name, change, nulls in weird:
                with self.subTest(case=name):
                    shutil.rmtree(repo.reviews)
                    shutil.rmtree(repo.markers, ignore_errors=True)
                    os.makedirs(repo.reviews)
                    content = dict(record(ATTEMPT, KEY), **change)
                    for key in [key for key, value in change.items() if value is None and key in ("judge",)]:
                        content.pop(key)
                    put_marker(repo, MANIFEST, ATTEMPT)
                    put_record(repo, KEY, ATTEMPT, content)
                    self.go(repo)
                    self.assertEqual(repo.lines()[-1]["reviews"], [dict(entry(), **nulls)])
            with self.subTest(case="missing containers"):
                shutil.rmtree(repo.reviews)
                shutil.rmtree(repo.markers)
                os.makedirs(repo.reviews)
                put_marker(repo, MANIFEST, ATTEMPT)
                put_record(repo, KEY, ATTEMPT, {"schema_version": 1, "attempt_id": ATTEMPT})
                self.go(repo)
                self.assertEqual(repo.lines()[-1]["reviews"], [{
                    "attempt_id": ATTEMPT, "review_key": KEY, "created": None, "verdict": None, "binding": None,
                    "contract_sha256": None, "handoff_sha256": None}])

    def test_an_absent_candidate_means_no_record_at_that_path(self):
        for repo in self.repos():
            os.makedirs(os.path.join(repo.reviews, "k0"))
            expected = add_review(repo, ATTEMPT, "k5")
            os.makedirs(os.path.join(repo.reviews, "k9"))
            self.go(repo)
            line = repo.lines()[0]
            self.assertEqual((line["reviews"], line["unmatched_markers"], line["reviews_state"]), ([expected], 0, "ok"))

    def test_a_candidate_that_is_not_a_regular_file_is_invalid(self):
        for repo in self.repos():
            valid = add_review(repo, ATTEMPT, "k5")
            elsewhere = os.path.join(self.tmp, "elsewhere-%s.json" % repo.kind)
            Path(elsewhere).write_text(json.dumps(record(ATTEMPT, "k1")))
            os.makedirs(os.path.join(repo.reviews, "k1"))
            os.symlink(elsewhere, os.path.join(repo.reviews, "k1", ATTEMPT + ".completed.json"))
            os.makedirs(os.path.join(repo.reviews, "k2", ATTEMPT + ".completed.json"))
            os.makedirs(os.path.join(repo.reviews, "k3"))
            os.symlink(os.path.join(self.tmp, "nowhere"), os.path.join(repo.reviews, "k3", ATTEMPT + ".completed.json"))
            self.go(repo)
            line = repo.lines()[0]
            self.assertEqual((line["reviews"], line["unmatched_markers"], line["reviews_state"]), ([valid], 0, "ok"))
            shutil.rmtree(os.path.join(repo.reviews, "k5"))
            self.go(repo)
            line = repo.lines()[-1]
            self.assertEqual((line["reviews"], line["unmatched_markers"], line["reviews_state"]), ([], 1, "ok"))

    def test_a_first_lstat_that_fails_with_another_error(self):
        for repo in self.repos():
            add_review(repo)
            real = os.lstat

            def failing(path, *args, **kwargs):
                if str(path).endswith(".completed.json"):
                    raise PermissionError(errno.EACCES, "injected")
                return real(path, *args, **kwargs)

            clock = Clock([100.0, 100.0, 100.0, 105.0])
            self.go(repo, clock=clock, patches=[mock.patch.object(co, "_lstat", failing)])
            line = repo.lines()[0]
            self.assertEqual((line["reviews"], line["reviews_state"], line["unmatched_markers"]), (None, "error", None))
            self.assertEqual(line["capture"]["state"], "ok")
            self.assertEqual(clock.calls, 4)
            self.assertEqual(line["elapsed_ms"], 5000)

    def test_not_a_directory_from_the_first_lstat_is_an_error(self):
        for repo in self.repos():
            add_review(repo)
            real = os.lstat

            def failing(path, *args, **kwargs):
                if str(path).endswith(".completed.json"):
                    raise NotADirectoryError(errno.ENOTDIR, "injected")
                return real(path, *args, **kwargs)

            self.go(repo, patches=[mock.patch.object(co, "_lstat", failing)])
            self.assertEqual(repo.lines()[0]["reviews_state"], "error")

    def test_a_record_that_disappears_between_lstat_and_open(self):
        for repo in self.repos():
            add_review(repo)
            opens = []

            def gone(path, *args):
                opens.append(path)
                raise FileNotFoundError(errno.ENOENT, "injected")

            self.go(repo, patches=[mock.patch.object(co, "_open_file", gone)])
            line = repo.lines()[0]
            self.assertEqual((line["reviews"], line["reviews_state"], line["unmatched_markers"]), (None, "error", None))
            self.assertEqual(len(opens), 1)
            self.assertTrue(opens[0].endswith(ATTEMPT + ".completed.json"))

    def test_an_io_error_after_a_regular_lstat_is_an_error(self):
        class Unreadable:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *args):
                raise OSError(errno.EIO, "injected")

        for repo in self.repos():
            add_review(repo)
            for name, replacement in (("open", mock.Mock(side_effect=OSError(errno.EIO, "injected"))),
                                      ("read", mock.Mock(return_value=Unreadable()))):
                with self.subTest(case=name):
                    self.go(repo, patches=[mock.patch.object(co, "_open_file", replacement)])
                    line = repo.lines()[-1]
                    self.assertEqual((line["reviews"], line["reviews_state"], line["unmatched_markers"]),
                                     (None, "error", None))

    def test_a_folder_listing_that_fails_is_an_error(self):
        for repo in self.repos():
            add_review(repo)
            real = os.listdir

            def failing(path):
                if str(path).endswith(MANIFEST):
                    raise PermissionError(errno.EACCES, "injected")
                return real(path)

            self.go(repo, patches=[mock.patch.object(co, "_listdir", failing)])
            self.assertEqual(repo.lines()[-1]["reviews_state"], "error")
            Path(repo.markers, "a" * 64).write_bytes(b"")
            self.go(repo, capture=lambda path, seconds: ok_result(manifest="a" * 64))
            self.assertEqual((repo.lines()[-1]["reviews_state"], repo.lines()[-1]["reviews"]), ("error", None))

            def failing_reviews(path):
                if str(path).endswith("reviews"):
                    raise PermissionError(errno.EACCES, "injected")
                return real(path)

            self.go(repo, patches=[mock.patch.object(co, "_listdir", failing_reviews)])
            self.assertEqual(repo.lines()[-1]["reviews_state"], "error")

    def test_a_marker_name_that_is_not_32_lower_case_hex_is_ignored(self):
        for repo in self.repos():
            valid = add_review(repo, "0123456789abcdef" * 2)
            names = ["a" * 31, "a" * 33, "A" * 32, "FEDCBA9876543210" * 2, "g" * 32, "a" * 31 + "é", ".tmp-" + "a" * 27,
                     "a" * 32 + ".x", " " + "a" * 31, "a" * 64]
            for name in names:
                put_marker(repo, MANIFEST, name)
                put_record(repo, KEY, name, record(name, KEY))
            self.go(repo)
            line = repo.lines()[0]
            self.assertEqual((line["reviews"], line["unmatched_markers"], line["reviews_state"]), ([valid], 0, "ok"))
            shutil.rmtree(os.path.join(repo.reviews, KEY))
            self.go(repo)
            self.assertEqual((repo.lines()[-1]["reviews"], repo.lines()[-1]["unmatched_markers"]), ([], 1))

    def test_a_stray_file_in_the_reviews_folder_is_not_a_key_folder(self):
        for repo in self.repos():
            valid = add_review(repo)
            Path(repo.reviews, ".DS_Store").write_bytes(b"x")
            Path(repo.reviews, "stray").write_bytes(b"x")
            self.go(repo)
            line = repo.lines()[0]
            self.assertEqual((line["reviews"], line["unmatched_markers"], line["reviews_state"]), ([valid], 0, "ok"))

    def test_a_marker_folder_that_is_a_file_is_an_error(self):
        for repo in self.repos():
            os.makedirs(repo.markers)
            Path(repo.markers, MANIFEST).write_bytes(b"")
            self.go(repo)
            self.assertEqual((repo.lines()[0]["reviews_state"], repo.lines()[0]["reviews"]), ("error", None))

    # ------------------------------------------------------------------ the budget

    def scenario(self, repo):
        add_review(repo, ATTEMPT, "k1")
        os.makedirs(os.path.join(repo.reviews, "k2"))

    def test_the_budget_ends_at_each_boundary(self):
        capture_timeout = {"state": "timeout", "reason": "budget ended before the capture", "manifest_sha256": None,
                           "index_sha256": None}
        capture_ok = {"state": "ok", "reason": None, "manifest_sha256": MANIFEST, "index_sha256": INDEX}
        cases = (("a", 1, 3, 0, 0, capture_timeout), ("b", 2, 4, 1, 0, capture_ok), ("c", 3, 5, 1, 0, capture_ok),
                 ("d", 4, 6, 1, 1, capture_ok))
        for repo in self.repos():
            for name, position, reads, captures, opens, capture in cases:
                with self.subTest(boundary=name):
                    shutil.rmtree(repo.readback)
                    os.makedirs(repo.reviews)
                    self.scenario(repo)
                    opened = []
                    real_open = open

                    def counting(path, *args):
                        opened.append(path)
                        return real_open(path, *args)

                    clock = Clock([100.0] * position + [105.0])
                    outcome = self.go(repo, clock=clock, patches=[mock.patch.object(co, "_open_file", counting)])
                    line = repo.lines()[-1]
                    self.assertEqual(line, expected_line(
                        capture=capture, reviews=None, reviews_state="timeout" if name != "a" else "no-identity",
                        unmatched_markers=None, elapsed_ms=5000))
                    self.assertEqual(clock.calls, reads)
                    self.assertEqual(len(outcome.captures), captures)
                    self.assertEqual(len(opened), opens)
                    if captures:
                        self.assertEqual(outcome.captures, [(repo.base, 5.0)])

    def test_the_budget_has_not_ended_just_before_the_limit(self):
        for repo in self.repos():
            for name, position, reads in (("a", 1, 6), ("b", 2, 6), ("c", 3, 6), ("d", 4, 6)):
                with self.subTest(boundary=name):
                    shutil.rmtree(repo.readback)
                    os.makedirs(repo.reviews)
                    self.scenario(repo)
                    clock = Clock([100.0] * position + [104.75])
                    outcome = self.go(repo, clock=clock)
                    line = repo.lines()[-1]
                    self.assertEqual(line, expected_line(reviews=[entry(key="k1")], elapsed_ms=4750))
                    self.assertEqual(clock.calls, reads)
                    self.assertEqual(outcome.captures, [(repo.base, 5.0 if name != "a" else 0.25)])

    def test_the_capture_gets_the_time_that_remains(self):
        for repo in self.repos():
            outcome = self.go(repo, clock=Clock([100.0, 101.25, 101.25]))
            self.assertEqual(outcome.captures, [(repo.base, 3.75)])
            outcome = self.go(repo, clock=Clock([100.0, 104.9921875, 105.0]))
            self.assertEqual(outcome.captures, [(repo.base, 5 - (104.9921875 - 100.0))])
            self.assertEqual(outcome.captures[0][1], 0.0078125)

    def test_the_budget_ends_exactly_at_five_seconds_before_the_capture(self):
        for repo in self.repos():
            outcome = self.go(repo, clock=Clock([100.0, 105.0]))
            self.assertEqual(outcome.captures, [])
            self.assertEqual(repo.lines()[-1]["capture"]["state"], "timeout")
            self.assertEqual(repo.lines()[-1]["capture"]["reason"], "budget ended before the capture")
            self.assertIsNone(repo.lines()[-1]["capture"]["manifest_sha256"])
            self.assertIsNone(repo.lines()[-1]["capture"]["index_sha256"])
            outcome = self.go(repo, clock=Clock([100.0, 110.0]))
            self.assertEqual(outcome.captures, [])
            self.assertEqual(repo.lines()[-1]["capture"]["state"], "timeout")

    def test_the_budget_ends_at_the_second_marker(self):
        for repo in self.repos():
            add_review(repo, "1" * 32)
            add_review(repo, "2" * 32)
            opened = []
            real_open = open

            def counting(path, *args):
                opened.append(path)
                return real_open(path, *args)

            clock = Clock([100.0] * 4 + [105.0])
            self.go(repo, clock=clock, patches=[mock.patch.object(co, "_open_file", counting)])
            line = repo.lines()[0]
            self.assertEqual((line["reviews"], line["reviews_state"], line["unmatched_markers"]), (None, "timeout", None))
            self.assertEqual(clock.calls, 6)
            self.assertEqual(len(opened), 1)

    def test_the_budget_ends_at_the_second_record_read_of_a_marker(self):
        for repo in self.repos():
            add_review(repo, ATTEMPT, "k1")
            put_record(repo, "k2", ATTEMPT, record(ATTEMPT, "k2"))
            opened = []
            real_open = open

            def counting(path, *args):
                opened.append(path)
                return real_open(path, *args)

            clock = Clock([100.0] * 4 + [105.0])
            self.go(repo, clock=clock, patches=[mock.patch.object(co, "_open_file", counting)])
            line = repo.lines()[0]
            self.assertEqual((line["reviews"], line["reviews_state"], line["unmatched_markers"]), (None, "timeout", None))
            self.assertEqual(clock.calls, 6)
            self.assertEqual(len(opened), 1)
            self.go(repo, clock=Clock([100.0]))
            self.assertEqual([item["review_key"] for item in repo.lines()[-1]["reviews"]], ["k1", "k2"])

    def test_an_absent_marker_folder_still_passes_boundary_d(self):
        for repo in self.repos():
            clock = Clock([100.0, 100.0, 105.0])
            self.go(repo, clock=clock)
            line = repo.lines()[0]
            self.assertEqual((line["reviews"], line["reviews_state"], line["unmatched_markers"], line["elapsed_ms"]),
                             (None, "timeout", None, 5000))
            self.assertEqual(clock.calls, 4)
            self.assertEqual(line["capture"]["state"], "ok")

    def test_no_marker_still_passes_boundary_d(self):
        for repo in self.repos():
            os.makedirs(os.path.join(repo.markers, MANIFEST))
            clock = Clock([100.0, 100.0, 105.0])
            self.go(repo, clock=clock)
            self.assertEqual((repo.lines()[0]["reviews_state"], repo.lines()[0]["reviews"]), ("timeout", None))
            self.assertEqual(clock.calls, 4)

    def test_a_lookup_exception_wins_over_the_end_of_the_budget(self):
        for repo in self.repos():
            add_review(repo)
            clock = Clock([100.0, 100.0, 100.0, 105.0])
            self.go(repo, clock=clock, patches=[mock.patch.object(co, "_lstat", side_effect=PermissionError(errno.EACCES, "x"))])
            line = repo.lines()[0]
            self.assertEqual((line["reviews_state"], line["reviews"], line["unmatched_markers"]), ("error", None, None))
            self.assertEqual(clock.calls, 4)
            self.assertEqual(line["elapsed_ms"], 5000)
            self.go(repo, clock=Clock([100.0, 100.0, 105.0]),
                    patches=[mock.patch.object(co, "_listdir", side_effect=PermissionError(errno.EACCES, "x"))])
            self.assertEqual(repo.lines()[-1]["reviews_state"], "error")

    def test_an_unknown_target_after_the_end_of_the_budget(self):
        for repo in self.repos():
            clock = Clock([100.0, 105.0])
            outcome = self.go(repo, words=[["cd", "x"], ["git", "commit"]], clock=clock)
            self.assertEqual(outcome.captures, [])
            self.assertEqual(clock.calls, 2)
            line = repo.lines()[0]
            self.assertEqual((line["capture"], line["reviews"], line["reviews_state"], line["unmatched_markers"],
                              line["elapsed_ms"], line["target"]), (None, None, "no-identity", None, 5000, "unknown"))

    # ------------------------------------------------------------------ elapsed_ms

    def test_elapsed_ms_truncates_a_fraction_of_a_millisecond(self):
        for repo in self.repos():
            for sample, expected in ((100.0 + 2 ** -9, 1), (100.0 + 2.0 + 2 ** -9, 2001), (100.0, 0), (100.0 + 2 ** -10, 0),
                                     (100.0 + 0.5, 500), (100.0 + 1.0 + 2 ** -10 * 3, 1002)):
                with self.subTest(sample=sample):
                    self.go(repo, clock=Clock([100.0, 100.0, 100.0, sample]))
                    self.assertEqual(repo.lines()[-1]["elapsed_ms"], expected)
                    self.assertIs(type(repo.lines()[-1]["elapsed_ms"]), int)

    def test_elapsed_ms_counts_from_the_start_of_the_budget(self):
        for repo in self.repos():
            self.go(repo, clock=Clock([7.0, 7.0, 7.0, 7.125]))
            self.assertEqual(repo.lines()[-1]["elapsed_ms"], 125)
            self.go(repo, clock=Clock([7.0, 8.0, 9.0, 11.5]))
            self.assertEqual(repo.lines()[-1]["elapsed_ms"], 4500)

    def test_the_elapsed_sample_comes_after_the_outcomes_and_before_the_write(self):
        for repo in self.repos():
            add_review(repo)
            log = []
            real_open = open
            real_serialize = co.serialize
            real_os_open = os.open
            real_fchmod, real_write = os.fchmod, os.write

            def capture(path, seconds):
                log.append("capture")
                return ok_result()

            def opener(path, *args):
                log.append("record read")
                return real_open(path, *args)

            def serialize(line):
                log.append("serialize")
                return real_serialize(line)

            def os_open(path, flags, mode=0o777):
                log.append("file open")
                return real_os_open(path, flags, mode)

            def fchmod(fd, mode):
                log.append("fchmod")
                real_fchmod(fd, mode)

            def write(fd, data):
                log.append("write")
                return real_write(fd, data)

            patches = [mock.patch.object(co, "_open_file", opener), mock.patch.object(co, "serialize", serialize),
                       mock.patch.object(co, "_os_open", os_open), mock.patch.object(co, "_fchmod", fchmod),
                       mock.patch.object(co, "_write", write)]
            clock = Clock([100.0], log)
            self.go(repo, clock=clock, capture=capture, patches=patches)
            self.assertEqual(log, ["clock", "clock", "capture", "clock", "clock", "record read", "clock", "clock",
                                   "serialize", "file open", "fchmod", "write"])

    # ------------------------------------------------------------------ the file

    def test_the_file_is_created_exclusively_with_mode_0600_and_written_once(self):
        for repo in self.repos():
            calls, writes, chmods = [], [], []
            real_os_open, real_write, real_fchmod = os.open, os.write, os.fchmod

            def os_open(path, flags, mode=None):
                calls.append((path, flags, mode))
                return real_os_open(path, flags) if mode is None else real_os_open(path, flags, mode)

            def write(fd, data):
                writes.append((fd, data))
                return real_write(fd, data)

            def fchmod(fd, mode):
                chmods.append((fd, mode))
                real_fchmod(fd, mode)

            patches = [mock.patch.object(co, "_os_open", os_open), mock.patch.object(co, "_write", write),
                       mock.patch.object(co, "_fchmod", fchmod)]
            self.go(repo, patches=patches)
            exclusive = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            self.assertEqual(calls, [(repo.file, exclusive, 0o600)])
            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0][1], repo.data())
            self.assertEqual(chmods, [(writes[0][0], 0o600)])
            with self.assertRaises(OSError):
                os.fstat(writes[0][0])
            calls.clear()
            writes.clear()
            chmods.clear()
            self.go(repo, patches=patches)
            appended = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
            self.assertEqual(calls, [(repo.file, exclusive, 0o600), (repo.file, appended, None)])
            self.assertEqual(len(writes), 1)
            self.assertEqual(chmods, [])
            self.assertEqual(len(repo.lines()), 2)
            with self.assertRaises(OSError):
                os.fstat(writes[0][0])

    def test_mode_0600_under_each_umask(self):
        for umask in (0o022, 0o077, 0o000, 0o277):
            for repo in self.repos():
                with self.subTest(umask=oct(umask)):
                    previous = os.umask(umask)
                    try:
                        self.go(repo)
                    finally:
                        os.umask(previous)
                    self.assertEqual(stat.S_IMODE(os.stat(repo.file).st_mode), 0o600)
                    self.assertTrue(repo.data().endswith(b"\n"))

    def test_the_mode_is_0600_after_a_successful_fchmod_under_umask_000(self):
        for repo in self.repos():
            previous = os.umask(0o000)
            self.addCleanup(os.umask, previous)
            real_os_open = os.open

            def lax(path, flags, mode=0o777):
                return real_os_open(path, flags, 0o666)

            self.go(repo, patches=[mock.patch.object(co, "_os_open", lax)])
            os.umask(previous)
            self.assertEqual(stat.S_IMODE(os.stat(repo.file).st_mode), 0o600)

    def test_an_existing_file_keeps_its_mode_and_its_earlier_bytes(self):
        for repo in self.repos():
            for mode in (0o640, 0o644, 0o600, 0o666):
                with self.subTest(mode=oct(mode)):
                    Path(repo.file).write_bytes(b"earlier line\nno newline at the end")
                    os.chmod(repo.file, mode)
                    before = repo.data()
                    previous = os.umask(0o077)
                    try:
                        self.go(repo)
                    finally:
                        os.umask(previous)
                    self.assertEqual(stat.S_IMODE(os.stat(repo.file).st_mode), mode)
                    self.assertTrue(repo.data().startswith(before))
                    self.assertEqual(repo.data(), before + line_bytes(expected_line()))
                    os.remove(repo.file)

    def test_a_second_call_appends_a_second_line(self):
        for repo in self.repos():
            self.go(repo)
            first = repo.data()
            self.go(repo, words=[["git", "commit", "--amend"]])
            self.assertTrue(repo.data().startswith(first))
            self.assertEqual(len(repo.lines()), 2)
            self.assertEqual(repo.lines()[1]["commit"]["amend"], True)

    def test_a_symlink_at_the_path_is_not_followed(self):
        for repo in self.repos():
            target = os.path.join(self.tmp, "target-" + repo.kind)
            Path(target).write_bytes(b"target\n")
            os.symlink(target, repo.file)
            outcome = self.go(repo)
            self.assertIsNone(outcome.path)
            self.assertEqual(Path(target).read_bytes(), b"target\n")
            self.assertTrue(os.path.islink(repo.file))
            os.remove(repo.file)
            dangling = os.path.join(self.tmp, "dangling-" + repo.kind)
            os.symlink(dangling, repo.file)
            self.assertIsNone(self.go(repo).path)
            self.assertFalse(os.path.exists(dangling))
            self.assertTrue(os.path.islink(repo.file))

    def test_a_directory_at_the_path_gets_no_line(self):
        for repo in self.repos():
            os.makedirs(repo.file)
            self.assertIsNone(self.go(repo).path)
            self.assertEqual(os.listdir(repo.file), [])

    def test_the_folder_cannot_be_written(self):
        if os.geteuid() == 0:
            self.skipTest("root can write a folder that has no write bit")
        for repo in self.repos():
            os.chmod(repo.readback, 0o500)
            self.addCleanup(os.chmod, repo.readback, 0o700)
            self.assertIsNone(self.go(repo).path)
            self.assertFalse(os.path.exists(repo.file))

    def test_fchmod_fails_on_a_new_file(self):
        for umask in (0o022, 0o077, 0o000, 0o277):
            for repo in self.repos():
                with self.subTest(umask=oct(umask)):
                    writes = []
                    previous = os.umask(umask)
                    try:
                        outcome = self.go(repo, patches=[
                            mock.patch.object(co, "_fchmod", side_effect=PermissionError(errno.EPERM, "x")),
                            mock.patch.object(co, "_write", writes.append)])
                    finally:
                        os.umask(previous)
                    self.assertIsNone(outcome.path)
                    self.assertEqual(writes, [])
                    self.assertEqual(repo.data(), b"")
                    self.assertEqual(stat.S_IMODE(os.stat(repo.file).st_mode), 0o600 & ~umask)

    def test_the_write_fails_before_a_byte(self):
        for repo in self.repos():
            outcome = self.go(repo, patches=[mock.patch.object(co, "_write", side_effect=OSError(errno.ENOSPC, "x"))])
            self.assertIsNone(outcome.path)
            self.assertEqual(repo.data(), b"")
            Path(repo.file).write_bytes(b"old\n")
            self.go(repo, patches=[mock.patch.object(co, "_write", side_effect=OSError(errno.ENOSPC, "x"))])
            self.assertEqual(repo.data(), b"old\n")

    def test_a_short_write_leaves_the_prefix_and_nothing_else(self):
        for repo in self.repos():
            calls = []
            real_write = os.write

            def short(fd, data):
                calls.append(len(data))
                return real_write(fd, data[:9])

            self.go(repo, patches=[mock.patch.object(co, "_write", short)])
            full = line_bytes(expected_line())
            self.assertEqual(repo.data(), full[:9])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0], len(full))

    def test_the_file_holds_no_argument_value(self):
        sentinels = {name: "SENT_%s_%s" % (name, "aFCctuSm") for name in ("MSG", "FILE", "AUTHOR", "DATE", "REV", "OPT",
                                                                           "PATH", "DIR")}
        for repo in self.repos():
            folder = os.path.join(repo.base, sentinels["DIR"])
            os.makedirs(folder)
            words = [["git", "-C", sentinels["DIR"], "-c", "k=" + sentinels["OPT"], "commit", "-m", sentinels["MSG"],
                      "-F", sentinels["FILE"], "--author", sentinels["AUTHOR"], "--date=" + sentinels["DATE"],
                      "--fixup", sentinels["REV"], "--trailer", sentinels["OPT"], "--", sentinels["PATH"]]]
            self.go(repo, words=words, event={"cwd": repo.base, "agent_type": "readback:worker", "session_id": "SENT_SESSION"})
            data = repo.data()
            self.assertEqual(len(repo.lines()), 1)
            self.assertEqual(repo.lines()[0]["commit"], cm(source="message", paths=1))
            for name, text in sentinels.items():
                self.assertNotIn(text.encode(), data, name)
            self.assertNotIn(b"SENT_", data)
            self.assertNotIn(repo.base.encode(), data)
            self.assertNotIn(b"SENT_SESSION", data)
            self.assertNotIn(b"readback:worker", data)

    def test_a_failed_line_holds_no_argument_value(self):
        for repo in self.repos():
            command = "git commit -m 'SENT_MSG_aFCctuSm -F SENT_FILE_aFCctuSm"
            self.go(repo, words=[], whole=False, command=command)
            self.assertEqual(len(repo.lines()), 1)
            self.assertNotIn(b"SENT_", repo.data())
            self.assertNotIn(b"git commit", repo.data())
            self.assertNotIn(repo.base.encode(), repo.data())

    def test_the_line_is_ascii_only(self):
        for repo in self.repos():
            add_review(repo, created="2026-11-16T10:00:00Z é \u2028", verdict="PASS é")
            self.go(repo)
            data = repo.data()
            data.decode("ascii")
            self.assertEqual(data.count(b"\n"), 1)
            self.assertEqual(repo.lines()[0]["reviews"][0]["created"], "2026-11-16T10:00:00Z é \u2028")


# ===================================================================================== HookTests

class HookBase(Bounded):
    deadline = 120

    @classmethod
    def setUpClass(cls):
        cls.folder = os.path.realpath(tempfile.mkdtemp(prefix="co-hook-"))
        cls.addClassCleanup(remove, cls.folder)
        cls.hooks = {}
        for kind in LAYOUTS:
            cls.hooks[kind] = build_layout(kind, os.path.join(cls.folder, kind))[0]
        cls.oracle = Oracle(base_hook(cls.folder))

    def run_hook(self, kind, event=None, env=None, cwd=None, raw=None, hook=None):
        return run_process([sys.executable, hook or self.hooks[kind]], event, env=env, cwd=cwd, raw=raw)

    def same_as_base(self, result, event=None, env=None, cwd=None, raw=None):
        return differences(result, self.oracle.result(event, raw=raw, env=env, cwd=cwd))


class HookTests(HookBase):
    """The hook as a process, in the plugin layout and the portable layout, against the hook at the base commit."""

    def test_the_comparison_rejects_a_difference(self):
        reference = Result(b"a", b"b", 0)
        self.assertEqual(differences(reference, Result(b"a", b"b", 0)), [])
        self.assertEqual(differences(reference, Result(b"x", b"b", 0)), ["stdout"])
        self.assertEqual(differences(reference, Result(b"a", b"x", 0)), ["stderr"])
        self.assertEqual(differences(reference, Result(b"a", b"b", 1)), ["returncode"])
        self.assertEqual(differences(Result(b"\xff", b"", 0), Result(b"\xfe", b"", 0)), ["stdout"])
        self.assertEqual(differences(Result(b"", b"\xff", 0), Result(b"", b"\xfe", 0)), ["stderr"])
        self.assertEqual(differences(Result(b"a\n", b"", 0), Result(b"a", b"", 0)), ["stdout"])
        outside = self.run_hook("plugin", bash_event("git reset --hard"))
        self.assertIsInstance(outside.stdout, bytes)
        self.assertIsInstance(outside.stderr, bytes)
        self.assertNotEqual(differences(outside, self.run_hook("plugin", bash_event("git status"))), [])

    def test_the_hook_at_the_base_commit_is_the_hook_of_the_base_commit(self):
        shown = subprocess.run(["git", "-C", str(ROOT), "rev-parse", BASE_COMMIT + ":hooks/guard_git.py"],
                               capture_output=True, check=True)
        digest = subprocess.run(["git", "hash-object", self.oracle.path], capture_output=True, check=True)
        self.assertEqual(shown.stdout, digest.stdout)
        self.assertNotIn(b"commit_observation", Path(self.oracle.path).read_bytes())

    def test_denied_and_allowed_lines_equal_the_base_hook(self):
        empty = new_tmp(self)
        jobs = [(kind, line) for kind in LAYOUTS for line in DENIED + ALLOWED]

        def job(item):
            kind, line = item
            return self.same_as_base(self.run_hook(kind, bash_event(line), cwd=empty), bash_event(line), cwd=empty)

        for (kind, line), found in zip(jobs, pooled(job, jobs)):
            with self.subTest(layout=kind, line=line):
                self.assertEqual(found, [])
        denied = self.run_hook("plugin", bash_event(DENIED[0]), cwd=empty)
        self.assertIn(b"permissionDecision", denied.stdout)

    def test_the_lines_c1_to_c46_equal_the_base_hook_and_write_the_line(self):
        rows = CONTRACT_LINES + [("C1-no-cwd", "git commit -m {M}", 1, "resolved", [], MSG)]
        jobs = [(kind, row) for kind in LAYOUTS for row in rows]

        def job(item):
            kind, row = item
            repo = commit_repo(self, name="r")
            event = bash_event(render(row[1], HOOK_VALUES), None if row[0].endswith("no-cwd") else repo.base)
            cwd = repo.base if row[0].endswith("no-cwd") else None
            result = self.run_hook(kind, event, cwd=cwd)
            return result, self.same_as_base(result, event, cwd=cwd), repo

        for (kind, row), (result, found, repo) in zip(jobs, pooled(job, jobs)):
            row_id, _template, count, target, _names, commit = row
            parse = parse_expected(row)
            with self.subTest(layout=kind, row=row_id):
                self.assertEqual(found, [])
                self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))
                lines = repo.lines()
                if parse is None:
                    self.assertEqual(lines, [])
                    self.assertFalse(os.path.exists(repo.file))
                    continue
                self.assertEqual(len(lines), 1)
                line = lines[0]
                self.assertEqual(sorted(line), FILE_KEYS)
                self.assertEqual(line["parse"], parse)
                if parse == "failed":
                    count, target, commit = None, "unknown", None
                self.assertEqual(line["commit_count"], count)
                self.assertEqual(line["commit"], commit)
                self.assertEqual(line["target"], target)
                self.assertIs(line["sub_agent"], False)
                self.assertEqual(line["schema_version"], 2)
                self.assertEqual(line["eligibility"], "UNVERIFIED")
                self.assertRegex(line["time"], r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
                if target == "resolved":
                    self.assertEqual(line["capture"]["state"], "ok")
                    self.assertRegex(line["capture"]["manifest_sha256"], r"\A[0-9a-f]{64}\Z")
                    self.assertRegex(line["capture"]["index_sha256"], r"\A[0-9a-f]{64}\Z")
                    self.assertEqual((line["reviews"], line["reviews_state"], line["unmatched_markers"]), ([], "ok", 0))
                else:
                    self.assertEqual((line["capture"], line["reviews"], line["reviews_state"], line["unmatched_markers"]),
                                     (None, None, "no-identity", None))

    def test_the_extra_forms_reach_the_file_the_same_way(self):
        rows = [row for row in EXTRA_LINES if row[0] in (
            "X-all-twice", "X-consumed-dashdash", "X-cluster-rest-then-word", "X-git-dir-next", "X-cd-in-a-shell",
            "X-substitution-first-in-walk-order", "X-eval-after-assignment", "X-bash-lc", "X-nested-shell",
            "X-C-takes-commit", "X-two-substitutions", "X-pathspec-from-file-next", "X-sudo")]
        jobs = [(kind, row) for kind in LAYOUTS for row in rows]

        def job(item):
            kind, row = item
            repo = commit_repo(self, name="r")
            event = bash_event(render(row[1], HOOK_VALUES), repo.base)
            return self.same_as_base(self.run_hook(kind, event), event), repo

        for (kind, row), (found, repo) in zip(jobs, pooled(job, jobs)):
            with self.subTest(layout=kind, row=row[0]):
                self.assertEqual(found, [])
                lines = repo.lines()
                self.assertEqual(len(lines), 1 if row[2] else 0)
                if row[2]:
                    self.assertEqual((lines[0]["commit_count"], lines[0]["commit"], lines[0]["target"]),
                                     (row[2], row[5], row[3]))

    def test_one_line_for_a_commit_call_with_a_resolved_target(self):
        for kind in LAYOUTS:
            for repo_kind in KINDS:
                with self.subTest(layout=kind, repo=repo_kind):
                    repo = commit_repo(self, repo_kind)
                    event = bash_event("git commit -am hello", repo.base)
                    result = self.run_hook(kind, event)
                    self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))
                    self.assertEqual(self.same_as_base(result, event), [])
                    lines = repo.lines()
                    self.assertEqual(len(lines), 1)
                    self.assertEqual(lines[0]["commit"], cm(everything=True, source="message"))
                    self.assertEqual(lines[0]["target"], "resolved")
                    self.assertEqual(lines[0]["capture"]["state"], "ok")
                    self.assertEqual(stat.S_IMODE(os.stat(repo.file).st_mode), 0o600)

    def test_sub_agent_true_and_false(self):
        for kind in LAYOUTS:
            for agent, expected in (("readback:worker", True), (None, False), ("", False), (5, False)):
                with self.subTest(layout=kind, agent=agent):
                    repo = commit_repo(self)
                    event = bash_event("git commit -m x", repo.base, agent_type=agent)
                    result = self.run_hook(kind, event)
                    self.assertEqual(self.same_as_base(result, event), [])
                    self.assertIs(repo.lines()[0]["sub_agent"], expected)
            repo = commit_repo(self)
            event = bash_event("git commit -m x", repo.base)
            self.run_hook(kind, event)
            self.assertIs(repo.lines()[0]["sub_agent"], False)

    def test_no_gate_no_line(self):
        for kind in LAYOUTS:
            with self.subTest(layout=kind):
                repo = commit_repo(self)
                shutil.rmtree(repo.reviews)
                event = bash_event("git commit -m x", repo.base)
                result = self.run_hook(kind, event)
                self.assertEqual(self.same_as_base(result, event), [])
                self.assertEqual(repo.lines(), [])
                self.assertEqual(os.listdir(repo.readback), [])
                outside = new_tmp(self)
                event = bash_event("git commit -m x", outside)
                self.assertEqual(self.same_as_base(self.run_hook(kind, event), event), [])
                self.assertEqual(os.listdir(outside), [])

    def test_a_denied_command_that_holds_a_commit_call_gets_no_line(self):
        for kind in LAYOUTS:
            for command in ("git commit -m x && git reset --hard", "git stash; git commit -m x",
                            "echo $(git commit -m x); git clean -fd", "bash -c 'git commit -m x; git stash'",
                            "git reset --hard; git commit -m 'x"):
                with self.subTest(layout=kind, command=command):
                    repo = commit_repo(self)
                    event = bash_event(command, repo.base)
                    result = self.run_hook(kind, event)
                    self.assertEqual(self.same_as_base(result, event), [])
                    self.assertIn(b'"permissionDecision": "deny"', result.stdout)
                    self.assertEqual(repo.lines(), [])
                    self.assertFalse(os.path.exists(repo.file))

    def tracer(self, kind, event=None, raw=None, env=None):
        """Run the hook in a layout whose two modules write a trace line when they are imported or used."""
        folder = new_tmp(self)
        hook, _ = build_layout(kind, folder, replace={"commit_observation": TRACE_COMMIT_OBSERVATION,
                                                      "review_capture": TRACE_REVIEW_CAPTURE})
        trace = os.path.join(folder, "trace.txt")
        result = run_process([sys.executable, hook], event, env=clean_env(TRACE=trace, **(env or {})), raw=raw)
        return result, (Path(trace).read_text() if os.path.exists(trace) else "")

    def test_the_tracer_sees_an_import_on_a_commit_call(self):
        for kind in LAYOUTS:
            with self.subTest(layout=kind):
                result, trace = self.tracer(kind, bash_event("git commit -m x", "/nowhere"))
                self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))
                lines = trace.splitlines()
                self.assertEqual(len(lines), 3)
                self.assertTrue(lines[0].startswith("commit_observation "))
                self.assertTrue(lines[1].startswith("review_capture "))
                self.assertEqual(lines[2], "observe " + repr([["git", "commit", "-m", "x"]]))

    def test_no_import_when_the_check_is_off(self):
        for kind in LAYOUTS:
            for value in ("off",):
                with self.subTest(layout=kind, value=value):
                    repo = commit_repo(self)
                    event = bash_event("git commit -m x", repo.base)
                    env = {"READBACK_COMMIT_CHECK": value}
                    result, trace = self.tracer(kind, event, env=env)
                    self.assertEqual(trace, "")
                    self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))
                    real = self.run_hook(kind, event, env=clean_env(**env))
                    self.assertEqual(self.same_as_base(real, event, env=clean_env(**env)), [])
                    self.assertEqual(repo.lines(), [])
                    self.assertFalse(os.path.exists(repo.file))

    def test_any_other_value_than_off_observes(self):
        for kind in LAYOUTS:
            for value in ("OFF", "Off", "off ", " off", "0", "false", "on", ""):
                with self.subTest(layout=kind, value=value):
                    event = bash_event("git commit -m x", "/nowhere")
                    result, trace = self.tracer(kind, event, env={"READBACK_COMMIT_CHECK": value})
                    self.assertEqual(len(trace.splitlines()), 3)
                    self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))

    def test_no_import_for_a_command_line_without_a_commit_call(self):
        lines = [row[1] for row in CONTRACT_LINES if parse_expected(row) is None] + [
            "git commit-graph write", "git log --grep commit", "git show commit", "git status", "echo 'x",
            "echo commit", "git -C commit", "", "ls -la", "git status commit", "git status 'x", "echo $(a 'x) b"]
        for kind in LAYOUTS:
            for line in lines:
                with self.subTest(layout=kind, line=line):
                    event = bash_event(render(line, VALUES), "/nowhere")
                    result, trace = self.tracer(kind, event)
                    self.assertEqual(trace, "")
                    self.assertEqual(self.same_as_base(result, event), [])

    def test_the_import_happens_exactly_for_a_candidate(self):
        rows = CONTRACT_LINES + [row for row in EXTRA_LINES if row[0] in (
            "X-heredoc-paren-in-a-message", "X-heredoc-quotes-in-a-message", "X-substitution-and-unbalanced-quote",
            "X-sudo", "X-bash-without-c", "X-C-takes-commit", "X-digit-before-input")]
        jobs = [(kind, row) for kind in LAYOUTS for row in rows]

        def job(item):
            kind, row = item
            event = bash_event(render(row[1], VALUES), "/nowhere")
            return self.tracer(kind, event)

        for (kind, row), (result, trace) in zip(jobs, pooled(job, jobs)):
            with self.subTest(layout=kind, row=row[0]):
                self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))
                self.assertEqual(len(trace.splitlines()), 3 if parse_expected(row) else 0)

    def test_a_failed_candidate_with_the_check_off_imports_nothing(self):
        for kind in LAYOUTS:
            repo = commit_repo(self)
            event = bash_event("git commit -m 'x", repo.base)
            result, trace = self.tracer(kind, event, env={"READBACK_COMMIT_CHECK": "off"})
            self.assertEqual(trace, "")
            self.assertEqual(self.same_as_base(result, event, env=clean_env(READBACK_COMMIT_CHECK="off")), [])
            real = self.run_hook(kind, event, env=clean_env(READBACK_COMMIT_CHECK="off"))
            self.assertEqual(self.same_as_base(real, event, env=clean_env(READBACK_COMMIT_CHECK="off")), [])
            self.assertEqual(repo.lines(), [])

    def test_failed_lines_go_to_the_repository_of_the_cwd(self):
        for kind in LAYOUTS:
            for repo_kind in KINDS:
                with self.subTest(layout=kind, repo=repo_kind):
                    repo = commit_repo(self, repo_kind)
                    event = bash_event(JSON_C54, os.path.join(repo.base, "p1"), agent_type="readback:worker")
                    result = self.run_hook(kind, event)
                    self.assertEqual(self.same_as_base(result, event), [])
                    line = repo.lines()[0]
                    self.assertEqual((line["parse"], line["commit_count"], line["commit"], line["target"], line["capture"],
                                      line["reviews"], line["reviews_state"], line["unmatched_markers"], line["sub_agent"]),
                                     ("failed", None, None, "unknown", None, None, "no-identity", None, True))

    def test_no_import_and_no_line_for_a_denied_command_with_a_commit_call(self):
        for kind in LAYOUTS:
            event = bash_event("git commit -m x && git reset --hard", "/nowhere")
            result, trace = self.tracer(kind, event)
            self.assertEqual(trace, "")
            self.assertEqual(self.same_as_base(result, event), [])
            self.assertIn(b"permissionDecision", result.stdout)

    def test_no_line_and_no_import_for_a_tool_other_than_bash(self):
        events = [
            {"tool_name": "Read", "tool_input": {"command": "git commit -m x"}, "cwd": "/nowhere"},
            {"tool_name": "bash", "tool_input": {"command": "git commit -m x"}, "cwd": "/nowhere"},
            {"tool_name": "Edit", "tool_input": {"command": "git commit -m x"}},
            {"tool_input": {"command": "git commit -m x"}},
            {"tool_name": "Bash", "tool_input": {"command": 5}},
            {"tool_name": "Bash", "tool_input": {"command": ["git", "commit"]}},
            {"tool_name": "Bash", "tool_input": {"command": None}},
            {"tool_name": "Bash", "tool_input": {}},
            {"tool_name": "Bash", "tool_input": None},
            {"tool_name": "Bash"},
            ["Bash"],
            "git commit",
        ]
        raws = [b"not json", b"", b"{", b"null", b"\xff\xfe"]
        for kind in LAYOUTS:
            for event in events:
                with self.subTest(layout=kind, event=event):
                    result, trace = self.tracer(kind, event)
                    self.assertEqual(trace, "")
                    self.assertEqual(self.same_as_base(result, event), [])
            for raw in raws:
                with self.subTest(layout=kind, raw=raw):
                    result, trace = self.tracer(kind, raw=raw)
                    self.assertEqual(trace, "")
                    self.assertEqual(self.same_as_base(result, raw=raw), [])
        repo = commit_repo(self)
        event = {"tool_name": "Read", "tool_input": {"command": "git commit -m x"}, "cwd": repo.base}
        for kind in LAYOUTS:
            self.assertEqual(self.same_as_base(self.run_hook(kind, event), event), [])
            self.assertFalse(os.path.exists(repo.file))

    def test_the_guard_looks_in_its_own_folder_first_then_in_scripts_beside_it(self):
        runner = ("import runpy, sys\nhook = sys.argv[1]\nsys.argv = [hook]\nrunpy.run_path(hook, run_name='__main__')\n")
        event = bash_event("git commit -m x", "/nowhere")
        stubs = {"commit_observation": TRACE_COMMIT_OBSERVATION, "review_capture": TRACE_REVIEW_CAPTURE}
        cases = []
        # plugin layout with the modules in both folders: the folder of the hook wins
        folder = new_tmp(self)
        hook, _ = build_layout("plugin", folder, replace=stubs)
        for name, data in stubs.items():
            Path(folder, "hooks", name + ".py").write_bytes(data)
            Path(folder, "scripts", name + ".py").write_bytes(data.replace(b"+ os.path.dirname", b'+ "DECOY " + os.path.dirname'))
        cases.append((hook, "hooks", "DECOY"))
        # plugin layout with the modules beside only
        folder = new_tmp(self)
        hook, _ = build_layout("plugin", folder, replace=stubs)
        cases.append((hook, "scripts", None))
        # portable layout
        folder = new_tmp(self)
        hook, _ = build_layout("portable", folder, replace=stubs)
        cases.append((hook, None, None))
        for hook, expected, decoy in cases:
            with self.subTest(hook=hook):
                trace = os.path.join(os.path.dirname(os.path.dirname(hook)) if expected else os.path.dirname(hook), "trace.txt")
                work = new_tmp(self)
                result = subprocess.run([sys.executable, "-c", runner, hook], input=json.dumps(event).encode(),
                                        capture_output=True, env=clean_env(TRACE=trace), cwd=work, timeout=30)
                self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))
                text = Path(trace).read_text()
                path = ast.literal_eval(Path(trace + ".path").read_text())
                self.assertEqual(path[:2], [os.path.dirname(hook), os.path.join(os.path.dirname(os.path.dirname(hook)), "scripts")])
                self.assertEqual(path[2], "")
                self.assertIn(os.path.dirname(os.__file__), path)
                first = text.splitlines()[0]
                self.assertTrue(first.startswith("commit_observation "))
                if decoy:
                    self.assertNotIn(decoy, text)
                    self.assertEqual(first, "commit_observation " + os.path.dirname(hook))
                elif expected:
                    self.assertEqual(first, "commit_observation " + os.path.join(os.path.dirname(os.path.dirname(hook)), "scripts"))
                else:
                    self.assertEqual(first, "commit_observation " + os.path.dirname(hook))

    def test_no_pycache_folder_appears(self):
        for kind in LAYOUTS:
            with self.subTest(layout=kind):
                folder = new_tmp(self)
                hook, modules = build_layout(kind, folder)
                self.assertEqual(self.pycache(folder), [])
                repo = commit_repo(self)
                event = bash_event("git commit -m x", repo.base)
                result = run_process([sys.executable, hook], event, env=clean_env())
                self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))
                self.assertEqual(len(repo.lines()), 1)
                self.assertEqual(self.pycache(folder), [])
                # the check can fail: the same import without the setting writes bytecode
                control = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import commit_observation" % modules],
                                         capture_output=True, env=clean_env(), timeout=30)
                self.assertEqual(control.returncode, 0, control.stderr)
                self.assertNotEqual(self.pycache(folder), [])

    @staticmethod
    def pycache(folder):
        return sorted(os.path.join(path, name) for path, dirs, _ in os.walk(folder) for name in dirs if name == "__pycache__")


# ===================================================================================== FaultTests

WRAPPER = r'''
import atexit, errno, json, os, runpy, sys
spec = json.loads(os.environ["FAULT"])
hook = sys.argv[1]
here = os.path.dirname(os.path.abspath(hook))
sys.dont_write_bytecode = True
sys.path[:0] = [here, os.path.join(os.path.dirname(here), "scripts")]
if "umask" in spec:
    os.umask(spec["umask"])
import review_capture
import commit_observation as co

LOG = []
atexit.register(lambda: open(spec["log"], "w").write(json.dumps(LOG)))


class Clock:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        LOG.append("clock")
        return value


co._clock = Clock(spec.get("clock", [100.0]))
co._now = lambda: "2026-11-17T00:00:00Z"
RESULT = {"manifest_version": 1, "state": "ok", "reason": None, "manifest_sha256": spec["manifest"], "entries": 1,
          "index_sha256": spec["index"], "root": "/root"}


def fake(repo, seconds):
    LOG.append("capture")
    if spec.get("capture") == "raise":
        raise RuntimeError("injected")
    return dict(RESULT, **spec.get("result", {}))


review_capture.capture = fake
real_lstat = co._lstat


def lstat(path, *args, **kwargs):
    if str(path).endswith(".completed.json"):
        LOG.append("lstat")
        if spec.get("lstat_error"):
            raise PermissionError(errno.EACCES, "injected")
    return real_lstat(path, *args, **kwargs)


co._lstat = lstat
real_open_file = co._open_file


def opener(*args, **kwargs):
    LOG.append("open")
    if spec.get("open_error"):
        raise OSError(errno.EIO, "injected")
    return real_open_file(*args, **kwargs)


co._open_file = opener
real_os_open = co._os_open


def os_open(*args, **kwargs):
    LOG.append("file-open")
    return real_os_open(*args, **kwargs)


co._os_open = os_open
real_fchmod = co._fchmod


def fchmod(fd, mode):
    LOG.append("fchmod")
    if spec.get("fchmod_error"):
        raise PermissionError(errno.EPERM, "injected")
    return real_fchmod(fd, mode)


co._fchmod = fchmod
real_write = co._write
write = spec.get("write")


def logged_write(fd, data):
    LOG.append("write")
    if write == "raise":
        raise OSError(errno.ENOSPC, "injected")
    return real_write(fd, data[:int(write)] if write else data)


co._write = logged_write
sys.argv = [hook]
runpy.run_path(hook, run_name="__main__")
'''

FAULT_TIME = "2026-11-17T00:00:00Z"
CAPTURE_RAISED = {"state": "error", "reason": "capture raised", "manifest_sha256": None, "index_sha256": None}
NO_IDENTITY = {"reviews": None, "reviews_state": "no-identity", "unmatched_markers": None}
TIMED_OUT = {"reviews": None, "reviews_state": "timeout", "unmatched_markers": None, "elapsed_ms": 5000}
BEFORE_THE_CAPTURE = {"state": "timeout", "reason": "budget ended before the capture", "manifest_sha256": None,
                      "index_sha256": None}


def not_ok(state, reason):
    return {"state": state, "reason": reason, "manifest_sha256": None, "index_sha256": INDEX}


def current_umask():
    value = os.umask(0)
    os.umask(value)
    return value


def drop_markers(repo):
    shutil.rmtree(repo.markers)


def old_file(repo):
    Path(repo.file).write_bytes(b"old\n")
    os.chmod(repo.file, 0o640)


def readonly_folder(repo):
    os.chmod(repo.readback, 0o500)


# (name, spec, expected change of the line, what the file holds, setup, command)
FAULTS = [
    ("control", {}, {}, "line", None, None),
    ("capture raises", {"capture": "raise"}, dict(capture=CAPTURE_RAISED, **NO_IDENTITY), "line", None, None),
    ("capture unsupported", {"result": {"state": "unsupported", "reason": "gitlink in the index", "manifest_sha256": None}},
     dict(capture=not_ok("unsupported", "gitlink in the index"), **NO_IDENTITY), "line", None, None),
    ("capture timeout", {"result": {"state": "timeout", "reason": "time budget used up", "manifest_sha256": None}},
     dict(capture=not_ok("timeout", "time budget used up"), **NO_IDENTITY), "line", None, None),
    ("capture error", {"result": {"state": "error", "reason": "cannot read a file", "manifest_sha256": None}},
     dict(capture=not_ok("error", "cannot read a file"), **NO_IDENTITY), "line", None, None),
    ("budget ends at a", {"clock": [100.0, 105.0]}, dict(capture=BEFORE_THE_CAPTURE, elapsed_ms=5000, **NO_IDENTITY),
     "line", None, None),
    ("budget ends at b", {"clock": [100.0, 100.0, 105.0]}, TIMED_OUT, "line", None, None),
    ("budget ends at c", {"clock": [100.0, 100.0, 100.0, 105.0]}, TIMED_OUT, "line", None, None),
    ("budget ends at d", {"clock": [100.0, 100.0, 100.0, 100.0, 105.0]}, TIMED_OUT, "line", None, None),
    ("absent marker folder with the end of the budget", {"clock": [100.0, 100.0, 105.0]},
     dict(TIMED_OUT, reviews=None), "line", drop_markers, None),
    ("lookup exception with the end of the budget", {"clock": [100.0, 100.0, 100.0, 105.0], "lstat_error": True},
     dict(reviews=None, reviews_state="error", unmatched_markers=None, elapsed_ms=5000), "line", None, None),
    ("unknown target after the end of the budget", {"clock": [100.0, 105.0]},
     dict(target="unknown", capture=None, elapsed_ms=5000, **NO_IDENTITY), "line", None, "cd x && git commit -m x"),
    ("lookup raises", {"lstat_error": True}, dict(reviews=None, reviews_state="error", unmatched_markers=None), "line", None,
     None),
    ("record read fails", {"open_error": True}, dict(reviews=None, reviews_state="error", unmatched_markers=None), "line",
     None, None),
    ("fchmod fails under umask 022", {"fchmod_error": True, "umask": 0o022}, {}, "empty", None, None),
    ("fchmod fails under umask 077", {"fchmod_error": True, "umask": 0o077}, {}, "empty", None, None),
    ("fchmod fails under umask 000", {"fchmod_error": True, "umask": 0o000}, {}, "empty", None, None),
    ("fchmod fails under umask 277", {"fchmod_error": True, "umask": 0o277}, {}, "empty", None, None),
    ("fchmod succeeds under umask 000", {"umask": 0o000}, {}, "line", None, None),
    ("write fails before a byte", {"write": "raise"}, {}, "empty", None, None),
    ("write fails before a byte, file exists", {"write": "raise"}, {}, "old", old_file, None),
    ("short write", {"write": "7"}, {}, "prefix", None, None),
    ("existing file", {}, {}, "old+line", old_file, None),
    ("folder cannot be written", {}, {}, "absent", readonly_folder, None),
]

C, L, O = "clock", "lstat", "open"
HEAD = [C, C, "capture"]
FULL = HEAD + [C, L, C, O, C, C]
CREATE = ["file-open", "fchmod", "write"]
APPEND = ["file-open", "file-open", "write"]
EVENTS = {
    "control": FULL + CREATE,
    "capture raises": HEAD + [C] + CREATE,
    "capture unsupported": HEAD + [C] + CREATE,
    "capture timeout": HEAD + [C] + CREATE,
    "capture error": HEAD + [C] + CREATE,
    "budget ends at a": [C, C, C] + CREATE,
    "budget ends at b": HEAD + [C, C] + CREATE,
    "budget ends at c": HEAD + [C, L, C, C] + CREATE,
    "budget ends at d": FULL + CREATE,
    "absent marker folder with the end of the budget": HEAD + [C, C] + CREATE,
    "lookup exception with the end of the budget": HEAD + [C, L, C] + CREATE,
    "unknown target after the end of the budget": [C, C] + CREATE,
    "lookup raises": HEAD + [C, L, C] + CREATE,
    "record read fails": HEAD + [C, L, C, O, C] + CREATE,
    "fchmod fails under umask 022": FULL + CREATE[:2],
    "fchmod fails under umask 077": FULL + CREATE[:2],
    "fchmod fails under umask 000": FULL + CREATE[:2],
    "fchmod fails under umask 277": FULL + CREATE[:2],
    "fchmod succeeds under umask 000": FULL + CREATE,
    "write fails before a byte": FULL + CREATE,
    "write fails before a byte, file exists": FULL + APPEND,
    "short write": FULL + CREATE,
    "existing file": FULL + APPEND,
    "folder cannot be written": FULL + ["file-open"],
}

IMPORT_FAULTS = [
    ("commit_observation cannot be imported", {"replace": {"commit_observation": b"raise ImportError('injected')\n"}}),
    ("commit_observation is missing", {"drop": ("commit_observation",)}),
    ("commit_observation has a syntax error", {"replace": {"commit_observation": b"def (:\n"}}),
    ("review_capture cannot be imported", {"replace": {"review_capture": b"raise ImportError('injected')\n"}}),
    ("review_capture is missing", {"drop": ("review_capture",)}),
]


class FaultTests(HookBase):
    """Injected faults in a hook process, in both layouts, against the hook at the base commit."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.wrapper = os.path.join(cls.folder, "wrapper.py")
        Path(cls.wrapper).write_text(WRAPPER)

    def prepared(self, setup=None):
        repo = commit_repo(self)
        add_review(repo)
        if setup:
            setup(repo)
        return repo

    def faulted(self, kind, spec, setup=None, command=None):
        repo = self.prepared(setup)
        event = bash_event(command or "git commit -m x", repo.base)
        log = os.path.join(new_tmp(self), "events.json")
        env = clean_env(FAULT=json.dumps(dict(spec, manifest=MANIFEST, index=INDEX, log=log)))
        result = run_process([sys.executable, "-B", self.wrapper, self.hooks[kind]], event, env=env)
        repo.events = json.loads(Path(log).read_text())
        return result, self.same_as_base(result, event), repo

    def test_the_comparison_rejects_a_difference(self):
        self.assertEqual(differences(Result(b"x", b"", 0), Result(b"x", b"", 0)), [])
        self.assertEqual(differences(Result(b"x", b"", 0), Result(b"y", b"", 0)), ["stdout"])
        self.assertEqual(differences(Result(b"x", b"e", 0), Result(b"x", b"f", 0)), ["stderr"])
        self.assertEqual(differences(Result(b"x", b"", 0), Result(b"x", b"", 3)), ["returncode"])
        # a fault run that changes the output is caught: a hook that prints is not the hook at the base commit
        folder = new_tmp(self)
        hook, _ = build_layout("portable", folder)
        text = Path(hook).read_text()
        self.assertIn("def main():\n    try:", text)
        Path(hook).write_text(text.replace("def main():\n    try:", "def main():\n    print('x')\n    try:", 1))
        noisy = run_process([sys.executable, hook], bash_event("git status"))
        self.assertEqual(differences(noisy, self.oracle.result(bash_event("git status"))), ["stdout"])

    def test_faults_in_a_hook_run_that_otherwise_writes_one_line(self):
        jobs = [(kind, fault) for kind in LAYOUTS for fault in FAULTS]

        def job(item):
            kind, (name, spec, _change, _held, setup, command) = item
            return self.faulted(kind, spec, setup, command)

        old = b"old\n"
        for (kind, (name, spec, change, held, _setup, _command)), (result, found, repo) in zip(jobs, pooled(job, jobs)):
            with self.subTest(layout=kind, fault=name):
                if os.geteuid() == 0 and name == "folder cannot be written":
                    self.skipTest("root can write a folder that has no write bit")
                self.assertEqual(found, [])
                self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))
                self.assertEqual(repo.events, EVENTS[name])
                line = line_bytes(expected_line(**dict({"reviews": [entry()], "time": FAULT_TIME}, **change)))
                if held == "line":
                    self.assertEqual(repo.data(), line)
                elif held == "empty":
                    self.assertEqual(repo.data(), b"")
                    umask = spec["umask"] if "umask" in spec else current_umask()
                    self.assertEqual(stat.S_IMODE(os.stat(repo.file).st_mode), 0o600 & ~umask)
                elif held == "prefix":
                    self.assertEqual(repo.data(), line[:7])
                elif held == "old":
                    self.assertEqual(repo.data(), old)
                elif held == "old+line":
                    self.assertEqual(repo.data(), old + line)
                    self.assertEqual(stat.S_IMODE(os.stat(repo.file).st_mode), 0o640)
                else:
                    self.assertEqual(held, "absent")
                    self.assertFalse(os.path.exists(repo.file))
                if name == "fchmod succeeds under umask 000":
                    self.assertEqual(stat.S_IMODE(os.stat(repo.file).st_mode), 0o600)
                os.chmod(repo.readback, 0o700)

    def test_each_import_fault_alone_in_both_layouts(self):
        jobs = [(kind, fault) for kind in LAYOUTS for fault in IMPORT_FAULTS]

        def job(item):
            kind, (name, options) = item
            folder = new_tmp(self)
            hook, _ = build_layout(kind, folder, **options)
            repo = self.prepared()
            event = bash_event("git commit -m x", repo.base)
            result = run_process([sys.executable, hook], event)
            return result, self.same_as_base(result, event), repo

        for (kind, (name, _options)), (result, found, repo) in zip(jobs, pooled(job, jobs)):
            with self.subTest(layout=kind, fault=name):
                self.assertEqual(found, [])
                self.assertEqual((result.stdout, result.stderr, result.returncode), (b"", b"", 0))
                self.assertFalse(os.path.exists(repo.file))
        for kind in LAYOUTS:
            with self.subTest(layout=kind, fault="none"):
                result, found, repo = job((kind, ("none", {})))
                self.assertEqual(found, [])
                self.assertEqual(len(repo.lines()), 1)
                self.assertEqual(repo.lines()[0]["reviews_state"], "ok")

    def test_the_fault_harness_writes_a_full_line_without_a_fault(self):
        for kind in LAYOUTS:
            result, found, repo = self.faulted(kind, {})
            self.assertEqual(found, [])
            self.assertEqual(repo.events, EVENTS["control"])
            self.assertEqual(repo.data(), line_bytes(expected_line(reviews=[entry()], time=FAULT_TIME)))
            self.assertEqual(len(repo.data().splitlines()), 1)


# ===================================================================================== SyncTests

class SyncTests(Bounded):
    """The portable skill holds the module and the guard, byte-equal to their sources."""

    def test_the_portable_copies_are_byte_equal_to_their_sources(self):
        skill = ROOT / "portable" / "skills" / "readback" / "scripts"
        for source, copy in ((ROOT / "scripts" / "commit_observation.py", skill / "commit_observation.py"),
                             (ROOT / "hooks" / "guard_git.py", skill / "guard_git.py")):
            with self.subTest(copy=copy.name):
                self.assertTrue(copy.is_file())
                self.assertEqual(copy.read_bytes(), source.read_bytes())
                self.assertGreater(len(source.read_bytes()), 1000)
        self.assertIn(b"def observe(", (skill / "commit_observation.py").read_bytes())
        self.assertIn(b"def simple_commands(", (skill / "guard_git.py").read_bytes())

    def test_sync_check_exits_zero(self):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "sync.py"), "--check"], capture_output=True,
                                cwd=str(ROOT), timeout=60)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"portable skills are current\n", b""))

    def test_sync_lists_the_module(self):
        sync = load_module("sync_under_test", ROOT / "scripts" / "sync.py")
        files = sync.build()
        self.assertEqual(files["readback/scripts/commit_observation.py"],
                         (ROOT / "scripts" / "commit_observation.py").read_text(encoding="utf-8"))
        self.assertEqual(files["readback/scripts/guard_git.py"], (ROOT / "hooks" / "guard_git.py").read_text(encoding="utf-8"))

    def test_the_check_can_fail(self):
        tree = Path(new_tmp(self), "tree")
        for name in ("scripts", "hooks", "agents", "output-styles", "portable"):
            shutil.copytree(ROOT / name, tree / name, ignore=shutil.ignore_patterns("__pycache__"))
        sync = str(tree / "scripts" / "sync.py")
        fresh = subprocess.run([sys.executable, "-B", sync, "--check"], capture_output=True, cwd=str(tree), timeout=60)
        self.assertEqual((fresh.returncode, fresh.stdout), (0, b"portable skills are current\n"))
        copy = tree / "portable" / "skills" / "readback" / "scripts" / "commit_observation.py"
        copy.write_bytes(copy.read_bytes()[:-1])
        stale = subprocess.run([sys.executable, "-B", sync, "--check"], capture_output=True, cwd=str(tree), timeout=60)
        self.assertEqual(stale.returncode, 1)
        self.assertIn(b"readback/scripts/commit_observation.py", stale.stdout)
        copy.unlink()
        gone = subprocess.run([sys.executable, "-B", sync, "--check"], capture_output=True, cwd=str(tree), timeout=60)
        self.assertEqual(gone.returncode, 1)
        guard = tree / "portable" / "skills" / "readback" / "scripts" / "guard_git.py"
        guard.write_bytes(guard.read_bytes() + b"\n")
        both = subprocess.run([sys.executable, "-B", sync, "--check"], capture_output=True, cwd=str(tree), timeout=60)
        self.assertIn(b"readback/scripts/guard_git.py", both.stdout)


if __name__ == "__main__":
    unittest.main(failfast=True)
