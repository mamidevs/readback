#!/usr/bin/env python3
"""Preview or expire one repository's finished judge records after 30 days."""

import argparse
from contextlib import contextmanager, ExitStack
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
import time

sys.dont_write_bytecode = True
import judge_rounds
import review_receipt
from review_redaction import redact

RETENTION_DAYS = 30
DAY = 86400
DIGEST = re.compile(r"[0-9a-f]{64}")
ATTEMPT = re.compile(r"[0-9a-f]{32}")
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
MISSING = {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}


@contextmanager
def directory(path, parent=None):
    """Pin each directory by descriptor. Symlinks at any level are never traversed."""
    with ExitStack() as stack:
        anchor = Path(path)
        parts = anchor.parts
        current = parent
        if anchor.is_absolute():
            current = os.open(anchor.anchor, DIRECTORY)
            stack.callback(os.close, current)
            parts = parts[1:]
        try:
            for part in parts:
                if part == "..":
                    raise ValueError("parent path is not allowed")
                current = os.open(part, DIRECTORY, dir_fd=current)
                stack.callback(os.close, current)
        except OSError as exc:
            if exc.errno not in MISSING:
                raise
            current = None
        yield current


def file_stat(folder, name):
    try:
        return os.stat(name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return None


def json_record(folder, name):
    info = file_stat(folder, name)
    if not info or not stat.S_ISREG(info.st_mode):
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except OSError as exc:
        if exc.errno in MISSING:
            return None
        raise
    if not os.path.samestat(info, os.fstat(fd)):
        os.close(fd)
        return None
    with os.fdopen(fd, "rb") as stream:
        try:
            value = json.load(stream)
        except (ValueError, RecursionError):
            return None
        return value if isinstance(value, dict) else None


def digest(value):
    return isinstance(value, str) and bool(DIGEST.fullmatch(value))


def capture_shape(capture):
    if capture.get("state") == "ok":
        return digest(capture.get("manifest_sha256"))
    return (capture.get("state") in ("error", "not-git", "timeout", "unsupported")
            and capture.get("manifest_sha256") is None)


def old_date(value, cutoff):
    try:
        return (isinstance(value, str) and judge_rounds.TIME.fullmatch(value)
                and judge_rounds.seconds_of(value) <= cutoff)
    except (ValueError, OverflowError):
        return False


def worktree_roots(project):
    root = project["worktree_root"]
    roots = {root}
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", root,
         "worktree", "list", "--porcelain", "-z"], stdin=subprocess.DEVNULL, capture_output=True,
        timeout=5, env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    if result.returncode == 0:
        roots.update(os.fsdecode(field[9:]) for field in result.stdout.split(b"\0")
                     if field.startswith(b"worktree "))
    return roots | {redact(path) for path in roots}


class Sweep:
    def __init__(self, project, cutoff, apply):
        self.project, self.cutoff, self.apply = project, cutoff, apply
        self.counts = dict(receipts=0, markers=0, round_payloads=0)
        self.roots = None

    def old_file(self, folder, name):
        info = file_stat(folder, name)
        if info and stat.S_ISREG(info.st_mode) and info.st_mtime <= self.cutoff:
            return info
        return None

    def remove(self, folder, name, info, kind):
        latest = file_stat(folder, name)
        if (not latest or not os.path.samestat(latest, info) or latest.st_mtime_ns != info.st_mtime_ns
                or latest.st_ctime_ns != info.st_ctime_ns or latest.st_size != info.st_size):
            return False
        if self.apply:
            try:
                os.unlink(name, dir_fd=folder)
            except FileNotFoundError:
                return False
        self.counts[kind] += 1
        return True

    def receipt(self, folder, name, attempt):
        info = self.old_file(folder, name)
        record = json_record(folder, name) if info else None
        if not (record and type(record.get("schema_version")) is int and record["schema_version"] == 1
                and record.get("attempt_id") == attempt and old_date(record.get("created"), self.cutoff)
                and all(isinstance(record.get(k), dict) for k in ("repository", "contract", "handoff", "helper_version"))):
            return None, None
        if not (digest(record["helper_version"].get("file_sha256"))
                and all(digest(record[k].get("sha256")) and isinstance(record[k].get("path"), str)
                        for k in ("contract", "handoff"))):
            return None, None
        repository = record["repository"]
        if "git_common_dir_sha256" in repository:
            common = os.fsencode(os.path.realpath(self.project["git_common_dir"]))
            if repository["git_common_dir_sha256"] != hashlib.sha256(common).hexdigest():
                return None, None
        else:
            # Older receipts identify their worktree by its original path alone.
            if self.roots is None:
                self.roots = worktree_roots(self.project)
            root = repository.get("worktree_root")
            if not isinstance(root, str) or root not in self.roots:
                return None, None
        return record, info

    def review(self, store, folder, key, attempt):
        completed_name = attempt + review_receipt.COMPLETED_SUFFIX
        completed, info = self.receipt(folder, completed_name, attempt)
        if not (completed and completed.get("review_key") == key
                and all(isinstance(completed.get(k), dict)
                        for k in ("capture_before", "capture_after", "judge", "validation", "route"))
                and capture_shape(completed["capture_before"]) and capture_shape(completed["capture_after"])
                and digest(completed.get("rules_sha256"))
                and type(completed.get("exit_status")) is int
                and completed["judge"].get("verdict") in ("PASS", "FAIL", "none")
                and isinstance(completed.get("binding"), str)):
            return
        started_name = attempt + review_receipt.STARTED_SUFFIX
        start_info = file_stat(folder, started_name)
        if start_info:
            started, start_info = self.receipt(folder, started_name, attempt)
            if not (started and started.get("review_key") == key and "capture_after" not in started
                    and {"capture_before", "rules_sha256", "route"} <= started.keys()
                    and all(completed.get(k) == v for k, v in started.items())):
                return
        after = completed["capture_after"]
        if after.get("state") == "ok":
            with directory("by-manifest/" + after["manifest_sha256"], store) as markers:
                if markers is not None and file_stat(markers, attempt):
                    marker = self.old_file(markers, attempt)
                    if not marker or marker.st_size or not self.remove(markers, attempt, marker, "markers"):
                        return
        # Keep the completion as the recovery witness until all other files are gone.
        if start_info and not self.remove(folder, started_name, start_info, "receipts"):
            return
        self.remove(folder, completed_name, info, "receipts")

    def receipts(self, store):
        with directory("reviews", store) as reviews:
            for key in os.listdir(reviews) if reviews is not None else ():
                if not DIGEST.fullmatch(key):
                    continue
                with directory(key, reviews) as folder:
                    for name in os.listdir(folder) if folder is not None else ():
                        attempt, sep, suffix = name.partition(".")
                        if sep and suffix == "completed.json" and ATTEMPT.fullmatch(attempt):
                            self.review(store, folder, key, attempt)
        with directory("no-review", store) as skipped:
            for name in os.listdir(skipped) if skipped is not None else ():
                attempt, sep, suffix = name.partition(".")
                if sep and suffix == "json" and ATTEMPT.fullmatch(attempt):
                    record, info = self.receipt(skipped, name, attempt)
                    if (record and record.get("result") in ("off", "skipped")
                            and all(isinstance(record.get(k), dict) for k in ("validation", "call"))):
                        self.remove(skipped, name, info, "receipts")

    def rounds(self, folder):
        owned = False
        home, repo = self.project["home"], self.project["repository"]
        for name in os.listdir(folder):
            key, sep, suffix = name.partition(".")
            if not (sep and suffix == "json" and DIGEST.fullmatch(key)):
                continue
            ledger = json_record(folder, name)
            if not ledger:
                continue
            lineage, tier = ledger.get("lineage"), ledger.get("tier")
            if not (tier in ("step", "gate") and isinstance(lineage, str) and judge_rounds.LINEAGE.fullmatch(lineage)
                    and key == judge_rounds.chain_key(home, repo, lineage, tier)):
                continue
            identity = {"project_home": home, "repository": repo, "lineage": lineage, "tier": tier}
            if (judge_rounds.ledger_problem(ledger, identity)
                    and judge_rounds.ledger_problem(ledger, {k: redact(v) for k, v in identity.items()})):
                continue
            attempts = ledger["attempts"]
            ids = [item["attempt_id"] for item in attempts]
            if len(set(ids)) != len(ids):
                continue
            owned = True
            for item in attempts:
                attempt = item["attempt_id"]
                if not ATTEMPT.fullmatch(attempt) or not old_date(item["finished_at"], self.cutoff):
                    continue
                with directory(key + "/" + attempt, folder) as payload:
                    if payload is None:
                        continue
                    for text in ("handoff.txt", "observation.txt", "answer.txt"):
                        info = self.old_file(payload, text)
                        if info:
                            self.remove(payload, text, info, "round_payloads")
        return owned


def _run(project, apply, daily):
    now = time.time()
    sweep = Sweep(project, now - RETENTION_DAYS * DAY, apply)
    with ExitStack() as stack:
        common = project["git_common_dir"]
        store = stack.enter_context(directory(Path(common) / "readback")) if common else None
        book = stack.enter_context(directory(Path.home() / "logbook" / ".readback"))
        rounds = stack.enter_context(directory("rounds", book)) if book is not None else None
        if store is None and rounds is None:
            return sweep.counts
        if daily:
            # One clock per project identity. flock also releases on process death.
            identity = json.dumps([project["home"], project["repository"]]).encode()
            stamp = ".retention-" + hashlib.sha256(identity).hexdigest()
            clock_folder = store if store is not None else book
            if store is None and file_stat(clock_folder, stamp) is None:
                # Shared ledgers may all belong to other repositories. Create a project clock
                # only after identifying a matching ledger. A concurrent first sweep is idempotent.
                if not sweep.rounds(rounds):
                    return sweep.counts
                rounds = None
            fd = os.open(stamp, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600,
                         dir_fd=clock_folder)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return sweep.counts
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return sweep.counts
            last = os.read(fd, 32)
            if last and (not last.isdigit() or len(last) >= 32):
                return sweep.counts
            if last and now - DAY < int(last) <= now:
                return sweep.counts
        if store is not None:
            sweep.receipts(store)
        if rounds is not None:
            sweep.rounds(rounds)
        if daily:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, str(int(now)).encode("ascii"))
            os.ftruncate(fd, os.lseek(fd, 0, os.SEEK_CUR))
    return sweep.counts


def maybe_prune(project):
    """Best effort once per day, outside the provider's result and accounting path."""
    try:
        _run(project, apply=True, daily=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


def main(argv=None):
    import readback_settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("claude", "codex"), required=True)
    parser.add_argument("--apply", action="store_true", help="delete the eligible files")
    parser.add_argument("repository", nargs="?", default=".")
    args = parser.parse_args(argv)
    try:
        project = readback_settings.resolve(args.repository, args.host)
        counts = _run(project, args.apply, daily=False)
    except (OSError, ValueError, subprocess.SubprocessError, readback_settings.SettingsError):
        print("Could not complete the retention check for this repository.", file=sys.stderr)
        return 1
    print(json.dumps({"mode": "apply" if args.apply else "preview", "retention_days": RETENTION_DAYS, **counts}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
