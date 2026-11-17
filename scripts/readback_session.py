#!/usr/bin/env python3
"""Session state: the mode override of one session and the owner's round authorizations.

One state file per host and session: `<base>/<host>-<session id>.json`. The files are local and
untrusted; the agent runs as the same user and can write them. The module does not know who the
owner is. Callers use these functions and do not edit the files.

Public functions: read, set_mode, authorize, consume, clear, session_id_from_env. Errors: SessionError
with the code INVALID_ID (an unsafe session id) or INVALID_STATE (a mutation of an invalid file).
A wrong host, mode, tier, event id, time or field type in an argument raises ValueError.

Every mutation holds an exclusive flock on `<base>/<host>-<session id>.lock`, a file that stays. A state
is published by writing and syncing a temporary file in `<base>` with mode 0600, renaming it onto the
state file, then syncing the folder. `clear` unlinks the state file, then syncs the folder. `read` takes
no lock and looks only at the exact state file name. Folders that the module creates have mode 0700;
a folder that exists keeps its mode.

A crash after `consume` and before the caller stores its reservation loses that authorization. The
owner gives it again. The loss lowers the number of judge attempts and never raises it.
"""

import contextlib
import datetime
import fcntl
import json
import os
import re
import secrets
import stat

SCHEMA_VERSION = 1
DEFAULT_BASE = "~/logbook/.readback/sessions"
HOSTS = ("claude", "codex")
MODES = (None, "off", "gate", "each")
TIERS = ("step", "gate")
INVALID_ID = "INVALID_ID"
INVALID_STATE = "INVALID_STATE"
TARGET_FIELDS = ("project_home", "repository", "lineage", "tier", "settings_digest")
STATE_KEYS = ("schema_version", "host", "session_id", "mode", "mode_event", "authorizations", "processed_events")
AUTHORIZATION_KEYS = ("event_id", "at") + TARGET_FIELDS
TEMPORARY_PREFIX = ".tmp-"
ENV_NAMES = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_SESSION_ID"}

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_AT = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")


class SessionError(Exception):
    """A session state error. `code` is INVALID_ID or INVALID_STATE; str(error) is the code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# System calls that tests replace to inject failures.
_rename = os.rename


def _write(fd, data):
    """Write all bytes, sync them and close the descriptor."""
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_dir(folder):
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ------------------------------------------------------------------ names and checks

def safe_id(value):
    """True for a string that matches [A-Za-z0-9][A-Za-z0-9._-]{0,127} as a whole."""
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _check_host(host):
    if host not in HOSTS:
        raise ValueError("host")


def _names(host, session_id, base):
    """Validate host and id, return (folder, state file name, lock file name)."""
    _check_host(host)
    if not safe_id(session_id):
        raise SessionError(INVALID_ID)
    folder = os.path.abspath(os.path.expanduser(DEFAULT_BASE) if base is None else os.fspath(base))
    stem = host + "-" + session_id
    return folder, stem + ".json", stem + ".lock"


def session_id_from_env(host, env):
    """The session id of the host from env, when it is a safe id, else None."""
    _check_host(host)
    value = env.get(ENV_NAMES[host])
    return value if safe_id(value) else None


def _is_time(value):
    """A string of the form YYYY-MM-DDTHH:MM:SSZ that names a time that exists."""
    if not isinstance(value, str):
        return False
    match = _AT.fullmatch(value)
    if match is None:
        return False
    try:
        datetime.datetime(*(int(part) for part in match.groups()))
    except ValueError:
        return False
    return True


def _text(value):
    return isinstance(value, str) and value != ""


def _event_ok(event):
    return isinstance(event, dict) and set(event) == {"id", "at"} and _text(event["id"]) and _is_time(event["at"])


def _authorization_ok(item):
    if not isinstance(item, dict) or set(item) != set(AUTHORIZATION_KEYS):
        return False
    if not _text(item["event_id"]) or not _is_time(item["at"]):
        return False
    return all(isinstance(item[key], str) for key in TARGET_FIELDS) and item["tier"] in TIERS


def _problem(state, host, session_id):
    """The reason that a parsed file is not a valid state, or None."""
    if not isinstance(state, dict):
        return "not an object"
    for key in STATE_KEYS:
        if key not in state:
            return "missing key " + key
    for key in sorted(state):
        if key not in STATE_KEYS:
            return "unknown key " + key
    if type(state["schema_version"]) is not int or state["schema_version"] != SCHEMA_VERSION:
        return "schema_version"
    if state["host"] != host:
        return "host"
    if state["session_id"] != session_id:
        return "session_id"
    if state["mode"] not in MODES:
        return "mode"
    if state["mode_event"] is not None and not _event_ok(state["mode_event"]):
        return "mode_event"
    if not isinstance(state["authorizations"], list):
        return "authorizations"
    for index, item in enumerate(state["authorizations"]):
        if not _authorization_ok(item):
            return "authorizations[%d]" % index
    processed = state["processed_events"]
    if not isinstance(processed, list) or not all(_text(item) for item in processed):
        return "processed_events"
    return None


def _need_event(event_id, at):
    if not _text(event_id):
        raise ValueError("event_id")
    if not _is_time(at):
        raise ValueError("at")


def _fresh(host, session_id):
    return {
        "schema_version": SCHEMA_VERSION, "host": host, "session_id": session_id, "mode": None,
        "mode_event": None, "authorizations": [], "processed_events": [],
    }


# ----------------------------------------------------------------- files

def _load(folder, state_name, host, session_id):
    """("absent", None), ("valid", state) or ("invalid", reason) for the exact state file name."""
    path = os.path.join(folder, state_name)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "invalid", "unreadable"
    if not stat.S_ISREG(info.st_mode):
        return "invalid", "not a regular file"
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return "invalid", "unreadable"
    try:
        state = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return "invalid", "not JSON"
    reason = _problem(state, host, session_id)
    if reason is not None:
        return "invalid", reason
    return "valid", state


def _make_dirs(folder):
    """Create each missing level with mode 0700. A level that exists is left as it is."""
    parent = os.path.dirname(folder)
    if parent != folder and not os.path.isdir(parent):
        _make_dirs(parent)
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass


@contextlib.contextmanager
def _locked(folder, lock_name):
    """An exclusive flock on the lock file, which stays. Missing folders are created."""
    _make_dirs(folder)
    fd = os.open(os.path.join(folder, lock_name), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _publish(folder, state_name, state):
    """Write the state to a temporary file, sync it, rename it onto the state file, sync the folder."""
    data = (json.dumps(state, sort_keys=True, indent="  ") + "\n").encode("ascii")
    temporary = os.path.join(folder, TEMPORARY_PREFIX + secrets.token_hex())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write(fd, data)
        _rename(temporary, os.path.join(folder, state_name))
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    _sync_dir(folder)


def _writable(folder, state_name, host, session_id):
    """The state to change: a fresh one when there is no file. An invalid file raises INVALID_STATE."""
    kind, value = _load(folder, state_name, host, session_id)
    if kind == "invalid":
        raise SessionError(INVALID_STATE)
    return _fresh(host, session_id) if kind == "absent" else value


# ----------------------------------------------------------------- public functions

def read(host, session_id, base=None):
    """("absent", None), ("valid", state) or ("invalid", reason). Raises only for an unsafe id."""
    folder, state_name, _lock_name = _names(host, session_id, base)
    return _load(folder, state_name, host, session_id)


def set_mode(host, session_id, mode, event_id, at, base=None):
    """Write the mode and mode_event. The same event id as mode_event changes nothing."""
    folder, state_name, lock_name = _names(host, session_id, base)
    if mode not in MODES:
        raise ValueError("mode")
    _need_event(event_id, at)
    with _locked(folder, lock_name):
        state = _writable(folder, state_name, host, session_id)
        if state["mode_event"] is not None and state["mode_event"]["id"] == event_id:
            return
        state["mode"] = mode
        state["mode_event"] = {"id": event_id, "at": at}
        _publish(folder, state_name, state)


def authorize(host, session_id, event_id, at, project_home, repository, lineage, tier, settings_digest, base=None):
    """Append one authorization and remember its event id, unless the event id was stored before."""
    folder, state_name, lock_name = _names(host, session_id, base)
    _need_event(event_id, at)
    fields = {
        "project_home": project_home, "repository": repository, "lineage": lineage, "tier": tier,
        "settings_digest": settings_digest,
    }
    if not all(isinstance(value, str) for value in fields.values()):
        raise ValueError("field")
    if tier not in TIERS:
        raise ValueError("tier")
    with _locked(folder, lock_name):
        state = _writable(folder, state_name, host, session_id)
        if event_id in state["processed_events"]:
            return
        state["authorizations"].append(dict(fields, event_id=event_id, at=at))
        state["processed_events"].append(event_id)
        _publish(folder, state_name, state)


def consume(host, session_id, project_home, repository, lineage, tier, settings_digest, base=None):
    """Remove and return the earliest appended authorization whose five target fields equal the call's.

    Return None for no match, no file and an invalid file. The `at` values do not decide the order.
    """
    folder, state_name, lock_name = _names(host, session_id, base)
    if not os.path.isdir(folder):
        return None
    target = dict(zip(TARGET_FIELDS, (project_home, repository, lineage, tier, settings_digest)))
    with _locked(folder, lock_name):
        kind, state = _load(folder, state_name, host, session_id)
        if kind != "valid":
            return None
        for index, item in enumerate(state["authorizations"]):
            if all(item[key] == target[key] for key in TARGET_FIELDS):
                del state["authorizations"][index]
                _publish(folder, state_name, state)
                return item
    return None


def clear(host, session_id, base=None):
    """Remove the state file under the lock, valid or not. Quiet when there is none."""
    folder, state_name, lock_name = _names(host, session_id, base)
    if not os.path.isdir(folder):
        return
    with _locked(folder, lock_name):
        try:
            os.unlink(os.path.join(folder, state_name))
        except FileNotFoundError:
            return
        _sync_dir(folder)
