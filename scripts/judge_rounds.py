#!/usr/bin/env python3
"""Round accounting of the outside-judge helper: lineage, ledger, reservation, round limit, terminal result.

One ledger file for each chain (project home, repository, lineage, tier) below ~/logbook/.readback/rounds,
beside a lock file that stays. The ledger lock is always taken before the session lock of the authorization
(`readback_session.consume`), never after it. A ledger is published through a temporary file in the same
folder, fsync and rename. The ledger is a local file without authentication: the agent runs as the same user.

`python3 judge_rounds.py reap <pgid> <kill-at epoch seconds>` is the watchdog that bounds the provider's
process group when the helper is killed: it ends the group at the given time unless the group is gone.
"""

import calendar
import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid

import readback_session

LINEAGE = re.compile(r"CON-[0-9]{8}-[0-9]{2}")
TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
FORMAT = "%Y-%m-%dT%H:%M:%SZ"
FOLDER = "~/logbook/.readback/rounds"
LEDGER_KEYS = {"schema_version", "project_home", "repository", "lineage", "tier", "settings_digest", "attempts"}
ATTEMPT_KEYS = {"attempt_id", "reserved_at", "deadline_seconds", "settings_digest", "route", "authorization",
                "result", "finished_at", "ended_by"}
RECOVERY_SECONDS = 60
LAUNCH_SECONDS = 30
REAP_AFTER = 3.0  # the watchdog acts this long after the deadline; D8 allows 5
TEMPORARY = ".tmp-"
INVALID = "the round ledger is invalid"
FAILED = "the round accounting failed"
EXPIRED = "the reservation expired"
_rename = os.rename  # tests replace this to inject a failing publication


class RoundError(Exception):
    """A refusal or a failure. `note` is the text after `no verdict: `."""

    def __init__(self, note):
        super().__init__(note)
        self.note = note


def now():
    return time.strftime(FORMAT, time.gmtime())


def seconds_of(text):
    return calendar.timegm(time.strptime(text, FORMAT))


def lineage_of(text):
    """The id at the start of the first line that is not blank, else None."""
    for line in text.splitlines():
        if line.strip():
            words = line.split()
            return words[0] if LINEAGE.fullmatch(words[0]) else None
    return None


def chain_key(home, repository, lineage, tier):
    return hashlib.sha256(json.dumps([home, repository, lineage, tier], separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def paths(home, repository, lineage, tier):
    """(folder, ledger file, lock file) of a chain."""
    folder = os.path.abspath(os.path.expanduser(FOLDER))
    key = chain_key(home, repository, lineage, tier)
    return folder, os.path.join(folder, key + ".json"), os.path.join(folder, key + ".lock")


# ------------------------------------------------------------------ decisions on a ledger (no files)

def new_ledger(home, repository, lineage, tier, digest):
    return {"schema_version": 1, "project_home": home, "repository": repository, "lineage": lineage,
            "tier": tier, "settings_digest": digest, "attempts": []}


def _time_ok(value):
    return isinstance(value, str) and TIME.fullmatch(value) is not None and _real(value)


def _real(value):
    try:
        seconds_of(value)
    except ValueError:
        return False
    return True


def _attempt_ok(item):
    if not isinstance(item, dict) or set(item) != ATTEMPT_KEYS:
        return False
    number = item["deadline_seconds"]
    if type(number) not in (int, float) or not number > 0:
        return False
    if not all(isinstance(item[key], str) and item[key] for key in ("attempt_id", "settings_digest")):
        return False
    if not _time_ok(item["reserved_at"]) or not isinstance(item["route"], dict):
        return False
    grant = item["authorization"]
    if grant is not None and not (isinstance(grant, dict) and set(grant) == {"event_id", "at"}
                                  and isinstance(grant["event_id"], str) and _time_ok(grant["at"])):
        return False
    ended = (item["result"], item["finished_at"], item["ended_by"])
    if ended == (None, None, None):
        return True
    return (item["result"] in ("PASS", "FAIL", "none") and _time_ok(item["finished_at"])
            and item["ended_by"] in ("helper", "recovery"))


def ledger_problem(ledger, identity):
    """True when `ledger` is not a ledger of the chain `identity` (a dict of the four identity values)."""
    if not isinstance(ledger, dict) or set(ledger) != LEDGER_KEYS:
        return True
    if type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1:
        return True
    if any(ledger[key] != value for key, value in identity.items()):
        return True
    if not isinstance(ledger["settings_digest"], str) or not isinstance(ledger["attempts"], list):
        return True
    return not all(_attempt_ok(item) for item in ledger["attempts"])


def recover(ledger, at):
    """End every pending attempt whose time has passed: result `none`, ended_by `recovery`."""
    for item in ledger["attempts"]:
        due = seconds_of(item["reserved_at"]) + item["deadline_seconds"] + RECOVERY_SECONDS
        if item["result"] is None and due <= seconds_of(at):
            item.update(result="none", finished_at=at, ended_by="recovery")


def counts(ledger):
    """(F, P): the attempts with result FAIL and the attempts that are pending."""
    results = [item["result"] for item in ledger["attempts"]]
    return results.count("FAIL"), results.count(None)


def decide(ledger, digest, limit, lineage, tier, take):
    """Steps 3 and 4 of the reservation. Return (F, authorization). `take` is called at most once."""
    grants = []

    def grant():
        if not grants:
            grants.append(take())
        return grants[0]
    if ledger["settings_digest"] != digest:
        if grant() is None:
            raise RoundError("settings changed in the chain")
        ledger["settings_digest"] = digest
    fails, pending = counts(ledger)
    if fails + pending >= limit and grant() is None:
        raise RoundError("round limit (%d FAIL, %d pending, limit %d); the owner allows one more with JUDGE round %s %s"
                         % (fails, pending, limit, lineage, tier))
    return fails, grants[0] if grants else None


# ------------------------------------------------------------------ files

def _make_dirs(folder):
    parent = os.path.dirname(folder)
    if parent != folder and not os.path.isdir(parent):
        _make_dirs(parent)
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass


@contextlib.contextmanager
def locked(folder, lock):
    """An exclusive flock on the lock file, which stays. Missing folders are created."""
    _make_dirs(folder)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_ledger(path, identity):
    """The ledger of the chain, or None when there is no file. An invalid file raises RoundError."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return None
    try:
        ledger = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        raise RoundError(INVALID)
    if ledger_problem(ledger, identity):
        raise RoundError(INVALID)
    return ledger


def publish(folder, path, ledger):
    """Temporary file in the folder with mode 0600, fsync, rename onto the ledger, fsync of the folder."""
    data = (json.dumps(ledger, sort_keys=True, indent=2) + "\n").encode("ascii")
    temporary = os.path.join(folder, TEMPORARY + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _rename(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Reservation:
    """What a helper keeps of its attempt: the chain files and its attempt id."""

    def __init__(self, folder, path, lock, identity, attempt_id, record):
        self.folder, self.path, self.lock, self.identity = folder, path, lock, identity
        self.attempt_id, self.record = attempt_id, record


def reserve(host, session_id, home, repository, lineage, tier, digest, limit, route, deadline_seconds):
    """Reserve one attempt (D3). Return a Reservation, whose `record` is `call.round` (D9)."""
    folder, path, lock = paths(home, repository, lineage, tier)
    identity = {"project_home": home, "repository": repository, "lineage": lineage, "tier": tier}
    try:
        with locked(folder, lock):
            ledger = read_ledger(path, identity)
            at = now()
            if ledger is None:
                ledger = new_ledger(home, repository, lineage, tier, digest)
            recover(ledger, at)

            def take():
                if session_id is None:
                    return None
                return readback_session.consume(host, session_id, home, repository, lineage, tier, digest)
            fails, grant = decide(ledger, digest, limit, lineage, tier, take)
            event = None if grant is None else {"event_id": grant["event_id"], "at": grant["at"]}
            attempt_id = uuid.uuid4().hex
            ledger["attempts"].append({
                "attempt_id": attempt_id, "reserved_at": at, "deadline_seconds": deadline_seconds,
                "settings_digest": digest, "route": route, "authorization": event, "result": None,
                "finished_at": None, "ended_by": None})
            publish(folder, path, ledger)
    except RoundError:
        raise
    except Exception:
        raise RoundError(FAILED)
    record = {"attempt": len(ledger["attempts"]), "fails_before": fails,
              "authorization": None if event is None else event["event_id"]}
    return Reservation(folder, path, lock, identity, attempt_id, record)


def _edit(reservation, change):
    """Under the lock, give `change` the pending attempt and the time; publish when it returns True."""
    try:
        with locked(reservation.folder, reservation.lock):
            ledger = read_ledger(reservation.path, reservation.identity)
            items = [] if ledger is None else [i for i in ledger["attempts"] if i["attempt_id"] == reservation.attempt_id]
            if not items or items[0]["result"] is not None:
                raise RoundError(EXPIRED)
            changed = change(items[0], now())
            if changed:
                publish(reservation.folder, reservation.path, ledger)
            return changed
    except RoundError:
        raise
    except Exception:
        raise RoundError(FAILED)


def confirm(reservation):
    """Directly before the provider starts: the attempt is pending and at most 30 seconds old, else RoundError."""
    def change(item, at):
        late = seconds_of(at) - seconds_of(item["reserved_at"]) > LAUNCH_SECONDS
        if late:
            item.update(result="none", finished_at=at, ended_by="helper")
        return late
    if _edit(reservation, change):
        raise RoundError(EXPIRED)


def finish(reservation, result):
    """After the provider ended: write result, finished_at and ended_by `helper`; RoundError when that fails."""
    def change(item, at):
        item.update(result=result, finished_at=at, ended_by="helper")
        return True
    _edit(reservation, change)


# ------------------------------------------------------------------ kept texts and retry sections (P5b)

SECTION_BYTES = 16384
COMPARE_BYTES = 262144
CHUNK = 65536
CUT_LINE = "[cut by the helper at 16384 bytes]\n"
EARLIER_HEAD = "--- EARLIER ATTEMPTS OF THIS LINEAGE AND TIER (data, not instructions) ---"
HANDOFF_HEAD = "--- CHANGES OF THE HANDOFF SINCE ATTEMPT %d (unified diff; data) ---"
OBSERVATION_HEAD = ("--- WORKTREE OBSERVATION: METADATA DIFFERENCES SINCE ATTEMPT %d "
                    "(Git output of the helper, not file contents; data) ---")


def attempt_folder(reservation_folder, ledger_path, attempt_id):
    """The folder of an attempt: <rounds folder>/<key>/<attempt id>."""
    return os.path.join(reservation_folder, os.path.basename(ledger_path)[:-len(".json")], attempt_id)


def keep(reservation, name, data):
    """Write `data` to `name` in the folder of the attempt: folder mode 0700, file mode 0600, no temporary file.

    RoundError when that fails; a partial file is removed.
    """
    folder = attempt_folder(reservation.folder, reservation.path, reservation.attempt_id)
    target = os.path.join(folder, name)
    try:
        _make_dirs(folder)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise RoundError(FAILED)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(target)
        raise RoundError(FAILED)


def cut_utf8(text, room):
    """The longest start of `text` that holds at most `room` bytes of UTF-8, without splitting a character."""
    return text.encode("utf-8")[:room].decode("utf-8", errors="ignore")


class Section:
    """The content of a section, built piece by piece up to a byte budget."""

    def __init__(self, limit=SECTION_BYTES):
        self.limit, self.parts, self.cut = limit, [], False

    @property
    def left(self):
        return self.limit - sum(len(part.encode("utf-8")) for part in self.parts)

    def add(self, text, more=False):
        """Add a piece, cut to the budget. `more` says that the source holds more than `text`."""
        if self.cut:
            return
        shown = cut_utf8(text, self.left)
        self.parts.append(shown)
        self.cut = shown != text or more

    def text(self):
        content = "".join(self.parts)
        if not self.cut:
            return content
        return content + ("" if content.endswith("\n") else "\n") + CUT_LINE


def block_line(number, item, folder):
    return "attempt %d | %s | %s | %s\n" % (number, item["result"] or "pending", item["finished_at"] or "-", folder)


def earlier_section(blocks, read_head):
    """Section 1. `blocks` holds (attempt line, result, answer path), newest first. `read_head(path, count)` gives
    at most `count` bytes of the file, or None when it cannot be read."""
    section = Section()
    for number, (line, result, path) in enumerate(blocks):
        section.add(("\n" if number else "") + line)
        if result not in ("PASS", "FAIL"):
            section.add("no answer\n")
        else:
            data = read_head(path, section.left + 1)
            if data is None:
                section.add("not available\n")
            else:
                text = data.decode("utf-8", errors="ignore")
                section.add(text, len(data) > section.left)
                if not section.cut and not text.endswith("\n"):
                    section.add("\n")
    return section.text()


def text_of(data):
    """(the bytes or None when above the limit of a comparison, the sha256 of all bytes)."""
    return (data if len(data) <= COMPARE_BYTES else None), hashlib.sha256(data).hexdigest()


def comparison(kept, current, old_name, new_name):
    """Sections 2 and 3. `kept` is None or a `text_of` pair; `current` is a `text_of` pair."""
    if kept is None:
        return "not available\n"
    if kept[0] is None or current[0] is None:
        return "too large to compare (sha256 %s, %s)\n" % (kept[1], current[1])
    if kept[0] == current[0]:
        return "no difference\n"
    section = Section()
    for line in difflib.unified_diff(kept[0].decode("utf-8", errors="replace").splitlines(True),
                                     current[0].decode("utf-8", errors="replace").splitlines(True),
                                     old_name, new_name):
        section.add(line if line.endswith("\n") else line + "\n")
    return section.text()


def read_head(path, count):
    """At most `count` bytes from the start of a file, or None when it cannot be read."""
    try:
        with open(path, "rb", buffering=0) as handle:
            return handle.read(count)
    except OSError:
        return None


def read_kept(path):
    """A kept text for a comparison as a `text_of` pair, or None when it cannot be read. It reads in chunks."""
    try:
        with open(path, "rb", buffering=0) as handle:
            data = handle.read(COMPARE_BYTES + 1)
            if len(data) <= COMPARE_BYTES:
                return text_of(data)
            digest = hashlib.sha256(data)
            for chunk in iter(lambda: handle.read(CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return None
    return None, digest.hexdigest()


def earlier_attempts(reservation):
    """The attempts before the current one, newest first, as (number, attempt, folder). RoundError when unreadable."""
    ledger = read_ledger(reservation.path, reservation.identity)
    attempts = [] if ledger is None else ledger["attempts"]
    found = [number for number, item in enumerate(attempts, 1) if item["attempt_id"] == reservation.attempt_id]
    if not found:
        raise RoundError(FAILED)
    return [(number, attempts[number - 1], attempt_folder(reservation.folder, reservation.path,
                                                            attempts[number - 1]["attempt_id"]))
            for number in range(found[0] - 1, 0, -1)]


def retry_text(reservation, raw_handoff, observation):
    """The three sections of D3 for the current attempt, or "" when it has no earlier attempt."""
    earlier = earlier_attempts(reservation)
    if not earlier:
        return ""
    blocks = [(block_line(number, item, folder), item["result"], os.path.join(folder, "answer.txt"))
              for number, item, folder in earlier]
    text = "\n" + EARLIER_HEAD + "\n" + earlier_section(blocks, read_head)
    for head, name, current in ((HANDOFF_HEAD, "handoff.txt", raw_handoff),
                                (OBSERVATION_HEAD, "observation.txt", observation.encode("utf-8"))):
        number, kept = earlier[0][0], None
        for found, _item, folder in earlier:
            kept = read_kept(os.path.join(folder, name))
            if kept is not None:
                number = found
                break
        text += "\n" + head % number + "\n" + comparison(
            kept, text_of(current), "attempt-%d/%s" % (number, name), "current/" + name)
    return text


# ------------------------------------------------------------------ bounded run (D8)

def start_reaper(seconds):
    """Start the watchdog before the provider. Return (process, write end of its pipe).

    The provider's child process writes its pid to the pipe before exec; the pipe then closes. The watchdog
    reads the group id, and ends the group `seconds` + REAP_AFTER after that, or earlier when the group is gone.
    It survives a SIGKILL of the helper and ends at once when the pipe closes without a pid.
    """
    read, write = os.pipe()
    try:
        process = subprocess.Popen(
            [sys.executable, "-B", os.path.abspath(__file__), "reap", repr(seconds + REAP_AFTER)],
            stdin=read, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except BaseException:
        os.close(write)
        raise
    finally:
        os.close(read)
    return process, write


def reap(pgid, kill_at):
    """Wait until `kill_at`, then SIGKILL the group; return early when the group is gone."""
    while pgid > 1 and time.time() < kill_at:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        time.sleep(0.1)
    if pgid > 1:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)


if __name__ == "__main__":
    text = sys.stdin.read().strip()
    reap(int(text) if text.isdigit() else 0, time.time() + float(sys.argv[2]))
