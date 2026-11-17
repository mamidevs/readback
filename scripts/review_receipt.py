#!/usr/bin/env python3
"""Review records: what a judge run looked at, stored as local files next to the Git data.

The module has two parts. The pure part builds values from its arguments: the review key, the
binding of two captures, the started and completed record objects, the judge object, the validation
phrase and the receipt line. It reads no file, starts no process and reads no clock. The store part
publishes record files and a marker below the Git common directory, lists attempts and files, and
reads the repository metadata, the clock and the helper file.

Layout below the Git common directory:

    readback/reviews/<review_key>/<attempt_id>.started.json
    readback/reviews/<review_key>/<attempt_id>.completed.json
    readback/by-manifest/<manifest_sha256>/<attempt_id>        (empty marker)

A record is written to a temporary name in its folder with O_EXCL, synced, linked to its final name,
and the temporary name is removed. A final name that exists is never replaced. Folders that the module
creates have mode 0700 and files 0600. A folder that exists keeps its mode.

The records are local and untrusted. They show what the helper stored, not that the provider ran once.
"""

import hashlib
import json
import os
import secrets
import stat
import subprocess
import time

SCHEMA_VERSION = 1
CAPTURE_SECONDS = 5
STARTED_SUFFIX = ".started.json"
COMPLETED_SUFFIX = ".completed.json"
UNAVAILABLE_IMPORT = "validator not importable"
UNAVAILABLE_RAISED = "validator raised"

_GIT_DROPPED = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
)


# ------------------------------------------------------------------ pure part

def sha256_hex(data):
    """The sha256 of bytes, as lower-case hex."""
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    """Sorted keys, separators `,` and `:`, UTF-8, non-ASCII text kept."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def review_key(contract_sha256, handoff_sha256, rules_sha256, route, helper_version, capture_before):
    """The key of a review. The manifest identity is the first capture's, or null when it is not ok."""
    manifest = capture_before["manifest_sha256"] if capture_before["state"] == "ok" else None
    return sha256_hex(canonical_json({
        "contract_sha256": contract_sha256, "handoff_sha256": handoff_sha256,
        "rules_sha256": rules_sha256, "route": route, "helper_version": helper_version,
        "manifest_sha256": manifest,
    }))


def binding(first, second):
    """`ends-equal` or why the two captures do not bind the review to one state of the files."""
    if first["state"] != "ok":
        return "unbound: before " + first["state"]
    if second["state"] != "ok":
        return "unbound: after " + second["state"]
    if first["manifest_sha256"] != second["manifest_sha256"]:
        return "unbound: changed"
    return "ends-equal"


def take_capture(capture, repo):
    """Call capture(repo, 5) and keep state, reason and manifest_sha256.

    An ordinary exception becomes the state `error`. KeyboardInterrupt and InterruptedError propagate.
    """
    try:
        result = capture(repo, CAPTURE_SECONDS)
        return {"state": result["state"], "reason": result["reason"], "manifest_sha256": result["manifest_sha256"]}
    except InterruptedError:
        raise
    except Exception:
        return {"state": "error", "reason": "capture raised", "manifest_sha256": None}


def run_validation(validate, contract_text, handoff_text):
    """The validator's object, or `unavailable` when there is no validator or it raises."""
    if validate is None:
        return {"valid": "unavailable", "reason": UNAVAILABLE_IMPORT}
    try:
        return validate(contract_text, handoff_text)
    except InterruptedError:
        raise
    except Exception:
        return {"valid": "unavailable", "reason": UNAVAILABLE_RAISED}


def validation_phrase(validation):
    """One of: validation true, validation false <n>, validation unparsed, validation unavailable."""
    valid = validation["valid"]
    if valid is True:
        return "validation true"
    if valid is False:
        return "validation false " + str(sum(1 for item in validation["findings"] if item["level"] == "defect"))
    if valid == "unparsed":
        return "validation unparsed"
    return "validation unavailable"


def judge_object(verdict, provider, requested_model, effort, models):
    """`models` is the collection of reported model names of a validated Claude answer, else None."""
    return {
        "verdict": verdict, "provider": provider, "requested_model": requested_model, "effort": effort,
        "reported_models": "unattested" if models is None else sorted(set(models)),
    }


def answer_sha256(answer):
    """The sha256 of the UTF-8 bytes of the answer, or null without an answer."""
    return None if answer is None else sha256_hex(answer.encode("utf-8"))


def started_record(attempt_id, key, created, repository, capture_before, contract_path, contract_sha256,
                   handoff_path, handoff_sha256, rules_sha256, route, helper_version):
    return {
        "schema_version": SCHEMA_VERSION, "attempt_id": attempt_id, "review_key": key, "created": created,
        "repository": repository, "capture_before": capture_before,
        "contract": {"path": contract_path, "sha256": contract_sha256},
        "handoff": {"path": handoff_path, "sha256": handoff_sha256},
        "rules_sha256": rules_sha256, "route": route, "helper_version": helper_version,
    }


def completed_record(started, capture_after, binding_text, validation, judge, answer_digest, exit_status,
                     duplicates):
    """The started fields plus the fields of the end of the run."""
    record = dict(started)
    record.update({
        "capture_after": capture_after, "binding": binding_text, "validation": validation, "judge": judge,
        "answer_sha256": answer_digest, "exit_status": exit_status, "duplicates_of": duplicates,
    })
    return record


def clean_field(text):
    """A line break becomes a space and `|` becomes `_`."""
    return text.replace("\n", " ").replace("\r", " ").replace("|", "_")


def receipt_line(attempt_id, binding_text, validation, completed_path):
    """The line for a stored completed record."""
    fields = (attempt_id, binding_text, validation_phrase(validation), completed_path)
    return "receipt    " + " | ".join(clean_field(field) for field in fields)


def none_line(reason):
    """The line when no completed record is stored."""
    return "receipt    none | " + clean_field(reason)


# ----------------------------------------------------------------- store part

def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def helper_version(path):
    """The version object of a helper file: the sha256 of its bytes."""
    with open(path, "rb") as handle:
        return {"file_sha256": sha256_hex(handle.read())}


def _git(repo, *args):
    env = {key: value for key, value in os.environ.items() if key not in _GIT_DROPPED}
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=CAPTURE_SECONDS, check=False, env=env,
        )
    except InterruptedError:
        raise
    except (OSError, subprocess.TimeoutExpired):
        return None, b""
    return result.returncode, result.stdout


def repository_info(repo):
    """root_name, worktree_root, head and branch of the worktree that holds repo, or None outside a worktree."""
    code, top = _git(repo, "rev-parse", "--show-toplevel")
    root = os.fsdecode(top[:-1])
    if code != 0:
        return None
    head_code, head = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD")
    branch_code, branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    return {
        "root_name": os.path.basename(root), "worktree_root": root,
        "head": os.fsdecode(head[:-1]) if head_code == 0 else None,
        "branch": os.fsdecode(branch[:-1]) if branch_code == 0 else None,
    }


def encode_record(record):
    return (json.dumps(record, sort_keys=True, indent="  ") + "\n").encode("ascii")


def _write(fd, data):
    """Write all bytes, sync them and close the descriptor."""
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _make_dirs(root, parts):
    """Create each missing level with mode 0700. A level that exists is left as it is."""
    path = root
    for part in parts:
        path = os.path.join(path, part)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    return path


def _publish(folder, name, data):
    """Write data to a temporary name, sync it, link it to folder/name and remove the temporary name."""
    final = os.path.join(folder, name)
    temporary = os.path.join(folder, ".tmp-" + secrets.token_hex())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write(fd, data)
        os.link(temporary, final)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return final


def _store(common, key, name, record):
    folder = _make_dirs(common, ("readback", "reviews", key))
    return _publish(folder, name, encode_record(record))


def store_started(common, record):
    """Store the started record and return its path. An OSError means that nothing new was stored."""
    return _store(common, record["review_key"], record["attempt_id"] + STARTED_SUFFIX, record)


def store_completed(common, record):
    """Store the completed record, then the marker when capture_after is ok.

    Return (path of the completed file, path of the marker or None). A completed record that cannot be
    stored raises OSError and no marker is written. A marker that cannot be stored gives None and never
    undoes the completed record.
    """
    path = _store(common, record["review_key"], record["attempt_id"] + COMPLETED_SUFFIX, record)
    after = record["capture_after"]
    if after["state"] != "ok":
        return path, None
    try:
        folder = _make_dirs(common, ("readback", "by-manifest", after["manifest_sha256"]))
        return path, _publish(folder, record["attempt_id"], b"")
    except InterruptedError:
        raise
    except OSError:
        return path, None


def _valid(path, attempt_id):
    """A regular file that parses as a JSON object with schema_version 1 and this attempt_id."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
        with open(path, "rb") as handle:
            data = json.load(handle)
    except (OSError, ValueError, RecursionError):
        return False
    version = data.get("schema_version") if isinstance(data, dict) else None
    return type(version) is int and version == SCHEMA_VERSION and data.get("attempt_id") == attempt_id


def _scan(folder):
    """One scan of a review folder: (valid started ids, valid completed ids, partial paths)."""
    started, completed, partial = [], [], []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        for suffix, found in ((STARTED_SUFFIX, started), (COMPLETED_SUFFIX, completed)):
            if name.endswith(suffix) and _valid(path, name[:-len(suffix)]):
                found.append(name[:-len(suffix)])
                break
        else:
            partial.append(path)
    return started, completed, partial


def duplicates_of(common, key, attempt_id):
    """Sorted ids of the valid completed records under the key at one scan, without attempt_id."""
    try:
        _started, completed, _partial = _scan(os.path.join(common, "readback", "reviews", key))
    except FileNotFoundError:
        return []
    return sorted(item for item in completed if item != attempt_id)


def list_unfinished(common):
    """Indeterminate attempts as (review_key, attempt_id) and partial files as paths.

    An attempt is indeterminate when it has a valid started record and no valid completed record. Any
    entry of a review folder that is not a valid record, and any entry of the reviews folder that is not
    a folder, is partial.
    """
    root = os.path.join(common, "readback", "reviews")
    indeterminate, partial = [], []
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return {"indeterminate": indeterminate, "partial": partial}
    for name in names:
        folder = os.path.join(root, name)
        if not stat.S_ISDIR(os.lstat(folder).st_mode):
            partial.append(folder)
            continue
        started, completed, extra = _scan(folder)
        indeterminate.extend((name, item) for item in started if item not in completed)
        partial.extend(extra)
    return {"indeterminate": indeterminate, "partial": partial}
