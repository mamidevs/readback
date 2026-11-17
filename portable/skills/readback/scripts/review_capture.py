#!/usr/bin/env python3
"""Name the exact bytes of a Git worktree, so a review record can say what a judge reviewed.

Run: python3 scripts/review_capture.py [--seconds N] [REPO]

The command prints one JSON object with sorted keys and exits 0 when it ran, whatever the
state. It exits 2 on a usage error.

Documented byte format of the identity:

    manifest_sha256 = sha256( b"readback-manifest-v1\\0" + record_1 + record_2 + ... )

    record = type + b" " + executable + b" " + hash + b" " + size + b"\\0" + path_bytes + b"\\0"

type, executable, hash and size are ASCII. The records are sorted by the raw bytes of the path.
No other byte enters the hash: no time, no owner, no group, no other mode bit, no root path.

    regular file   type "f", executable "x" when st_mode & 0o100 else "-", size in bytes,
                   hash = sha256 of the raw bytes of the file
    symlink        type "l", executable "-", size = length of the raw link target,
                   hash = sha256 of the raw link target
    absent         type "a", executable "-", hash "-", size "-"
                   (a tracked path that does not exist on disk)

The paths are the raw bytes of `git ls-files -z --cached` together with
`git ls-files -z --others --exclude-standard`, run at the worktree root, without duplicates.
A path is never decoded, quoted, normalised or case-folded.

`index_sha256` is the sha256 of the raw output of `git ls-files -s -z`. It is Git-normalized
and descriptive only. It is not an identity of the bytes on disk.

A capture that is not "ok" has no identity. States:
    ok            every path was read and no file changed during the capture
    unsupported   a gitlink, a directory (an untracked nested repository), or a path that is
                  neither a regular file nor a symlink
    error         a Git command failed, a file could not be read, or a file changed
    timeout       the time budget ran out
    not-git       the folder is not inside a Git worktree, or Git is not available

Limits, stated on purpose:
    - The domain is the Git path list. An untracked fifo, socket or device, and a nested repository
      that Git does not list, are outside it. A nested repository that Git lists gives "unsupported".
    - The compared stat fields are device, inode, size, mtime in nanoseconds and mode. Owner, group
      and ctime are not compared. Of the mode only the owner-executable bit (0o100) enters the identity.
    - The change detection is a sequence of rechecks, not an atomic snapshot. Each file is compared
      with its first stat through its descriptor before and after the read and through its path after
      the read, again for every listed path after the last file, and the two listings are run again
      and each is compared with its own first run. A change after a file's last recheck, or a change
      that restores every compared stat value, is not detected.
    - The clock is checked before each Git call, before each file, before each chunk, before each
      recheck, after the last recheck and, last, after the manifest hash. A clock that passes the
      deadline after the last check is not seen.
    - "The capture writes nothing" is the design requirement. The tests show it for a named matrix of
      cells, not for every possible repository.

The capture writes nothing. Git runs with --no-optional-locks and -c core.fsmonitor=false,
as an argument list and never through a shell, in an environment without the variables that
redirect Git (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY,
GIT_ALTERNATE_OBJECT_DIRECTORIES, GIT_COMMON_DIR, GIT_NAMESPACE, GIT_CEILING_DIRECTORIES, and every
variable whose name starts with GIT_TRACE), with GIT_OPTIONAL_LOCKS=0 and with GIT_TRACE=0,
GIT_TRACE2=0, GIT_TRACE2_PERF=0, GIT_TRACE2_EVENT=0, so that no trace file is written. Only read
commands run.
"""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time

MANIFEST_VERSION = 1
MAGIC = b"readback-manifest-v1\0"
CHUNK = 1 << 20
DEFAULT_SECONDS = 5.0

# Module-level names so that tests can replace the clock and the file system reads.
_clock = time.monotonic
_lstat = os.lstat
_readlink = os.readlink
_open = os.open
_fstat = os.fstat


_DROPPED_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
)


def _git_env():
    """Environment for every Git call: the repo argument decides the folder, not variables that redirect Git."""
    env = {key: value for key, value in os.environ.items()
           if key not in _DROPPED_ENV and not key.startswith("GIT_TRACE")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # An environment value overrides trace2.*Target in the Git configuration.
    for name in ("GIT_TRACE", "GIT_TRACE2", "GIT_TRACE2_PERF", "GIT_TRACE2_EVENT"):
        env[name] = "0"
    return env


class _Stop(Exception):
    def __init__(self, state, reason):
        super().__init__(reason)
        self.state = state
        self.reason = reason


def manifest_digest(entries):
    """Return the identity of entries: tuples (path, type, executable, hash, size).

    Path is bytes. The other fields are ASCII text, size may be an int. The function sorts by path bytes.
    """
    digest = hashlib.sha256(MAGIC)
    for path, kind, executable, file_hash, size in sorted(entries, key=lambda entry: entry[0]):
        record = (
            kind.encode("ascii") + b" " + executable.encode("ascii") + b" "
            + file_hash.encode("ascii") + b" " + str(size).encode("ascii")
            + b"\0" + path + b"\0"
        )
        digest.update(record)
    return digest.hexdigest()


def merge_paths(cached_output, others_output):
    """Join two `ls-files -z` outputs into one list of raw paths, without duplicates, sorted by bytes."""
    paths = set()
    for output in (cached_output, others_output):
        paths.update(part for part in output.split(b"\0") if part)
    return sorted(paths)


def _git(cwd, args, deadline):
    left = deadline - _clock()
    if left <= 0:
        raise _Stop("timeout", "time budget used up")
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(cwd), *args],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=left, check=False, env=_git_env(),
        )
    except subprocess.TimeoutExpired:
        raise _Stop("timeout", "git did not answer in time") from None
    except OSError:
        raise _Stop("not-git", "git is not available") from None
    return result.returncode, result.stdout


def _check(deadline):
    if _clock() >= deadline:
        raise _Stop("timeout", "time budget used up")


def _signature(info):
    return tuple(getattr(info, name) for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode"))


def _changed():
    return _Stop("error", "changed during capture")


def _read_file(path, expected, check):
    """Return (sha256 hex, byte count) of a regular file, read in chunks.

    The descriptor is compared with `expected` (the first lstat of the path) before and after the read.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = _open(path, flags)
    with os.fdopen(fd, "rb", buffering=0) as handle:
        info = _fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        if _signature(info) != _signature(expected):
            raise _changed()
        digest = hashlib.sha256()
        count = 0
        while True:
            check()
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
        if _signature(_fstat(handle.fileno())) != _signature(expected):
            raise _changed()
    return digest.hexdigest(), count


def _entry(path, root, tracked, deadline):
    """Return (entry, first lstat). The first lstat is None for a tracked path that is absent."""
    full = os.path.join(root, path)
    try:
        before = _lstat(full)
    except (FileNotFoundError, NotADirectoryError):
        if tracked:
            return (path, "a", "-", "-", "-"), None
        raise _Stop("error", "a path vanished during capture") from None
    except OSError:
        raise _Stop("error", "cannot stat a path") from None
    mode = before.st_mode
    if stat.S_ISDIR(mode):
        raise _Stop("unsupported", "directory or nested repository in the path list")
    if stat.S_ISLNK(mode):
        try:
            target = _readlink(full)
        except OSError:
            raise _Stop("error", "cannot read a link") from None
        kind, executable = "l", "-"
        file_hash, size = hashlib.sha256(target).hexdigest(), len(target)
    elif stat.S_ISREG(mode):
        try:
            file_hash, size = _read_file(full, before, lambda: _check(deadline))
        except OSError:
            raise _Stop("error", "cannot read a file") from None
        kind, executable = "f", ("x" if mode & 0o100 else "-")
    else:
        raise _Stop("unsupported", "path is neither a regular file nor a symlink")
    try:
        after = _lstat(full)
    except OSError:
        raise _changed() from None
    if _signature(before) != _signature(after) or (kind == "f" and size != before.st_size):
        raise _changed()
    return (path, kind, executable, file_hash, size), before


def _recheck(path, root, first):
    full = os.path.join(root, path)
    try:
        now = _lstat(full)
    except (FileNotFoundError, NotADirectoryError):
        if first is None:
            return
        raise _changed() from None
    except OSError:
        raise _Stop("error", "cannot stat a path") from None
    if first is None or _signature(now) != _signature(first):
        raise _changed()


def _collect(root, paths, tracked, deadline):
    """Read every path, then lstat every path again. Return the entries. root is bytes."""
    entries = []
    firsts = []
    for path in paths:
        _check(deadline)
        entry, first = _entry(path, root, path in tracked, deadline)
        entries.append(entry)
        firsts.append(first)
    for path, first in zip(paths, firsts):
        _check(deadline)
        _recheck(path, root, first)
    _check(deadline)
    return entries


def _split(output):
    return set(part for part in output.split(b"\0") if part)


def _listings(root, deadline):
    """Return the raw outputs of the --cached listing and of the --others listing."""
    code, cached = _git(root, ["ls-files", "-z", "--cached"], deadline)
    if code != 0:
        raise _Stop("error", "git ls-files failed")
    code, others = _git(root, ["ls-files", "-z", "--others", "--exclude-standard"], deadline)
    if code != 0:
        raise _Stop("error", "git ls-files failed")
    return cached, others


def _capture(repo, deadline, info):
    code, out = _git(repo, ["rev-parse", "--show-toplevel"], deadline)
    root_text = out[:-1] if out.endswith(b"\n") else out
    if code != 0 or not root_text:
        raise _Stop("not-git", "not inside a Git worktree")
    root = os.fsdecode(root_text)
    info["root"] = root

    code, index_out = _git(root, ["ls-files", "-s", "-z"], deadline)
    if code != 0:
        raise _Stop("error", "git ls-files failed")
    info["index_sha256"] = hashlib.sha256(index_out).hexdigest()
    if any(record.startswith(b"160000 ") for record in index_out.split(b"\0")):
        raise _Stop("unsupported", "gitlink in the index")

    cached, others = _listings(root, deadline)
    paths = merge_paths(cached, others)
    entries = _collect(os.fsencode(root), paths, _split(cached), deadline)
    cached_end, others_end = _listings(root, deadline)
    # Each listing on its own: a path that moves from one listing to the other is removed from one, added to the other.
    if _split(cached_end) != _split(cached) or _split(others_end) != _split(others):
        raise _Stop("error", "path list changed during capture")
    digest = manifest_digest(entries)
    _check(deadline)  # the last thing before an ok result
    info["entries"] = len(entries)
    info["manifest_sha256"] = digest


def capture(repo, seconds):
    """Return the capture of the worktree that holds repo, as a dict. See the module docstring."""
    deadline = _clock() + seconds
    info = {"root": None, "index_sha256": None, "entries": None, "manifest_sha256": None}
    state, reason = "ok", None
    try:
        _capture(repo, deadline, info)
    except _Stop as stop:
        state, reason = stop.state, stop.reason
        info["entries"] = None
        info["manifest_sha256"] = None
    return {
        "manifest_version": MANIFEST_VERSION,
        "state": state,
        "reason": reason,
        "manifest_sha256": info["manifest_sha256"],
        "entries": info["entries"],
        "index_sha256": info["index_sha256"],
        "root": info["root"],
    }


def git_common_dir(repo):
    """Return the absolute path of the Git common directory of repo, or None outside Git.

    The path is the one that `git rev-parse --git-common-dir` prints, resolved against the folder
    Git ran in, with symlinks resolved. A main worktree and a linked worktree of one repository
    give the same value.
    """
    folder = os.path.abspath(os.fspath(repo))
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", folder,
             "rev-parse", "--git-common-dir"],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=DEFAULT_SECONDS, check=False, env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = result.stdout[:-1] if result.stdout.endswith(b"\n") else result.stdout
    if result.returncode != 0 or not out:
        return None
    return os.path.realpath(os.path.join(folder, os.fsdecode(out)))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="review_capture.py", allow_abbrev=False,
        description="Print one JSON object that names the exact content of a Git worktree.",
    )
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                        help="time budget in seconds (default 5)")
    parser.add_argument("repo", nargs="?", default=".", help="folder inside the worktree (default .)")
    args = parser.parse_args(argv)
    if not (0 <= args.seconds < float("inf")):
        parser.error("--seconds must be a finite number that is not negative")
    print(json.dumps(capture(args.repo, args.seconds), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
