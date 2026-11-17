#!/usr/bin/env python3
"""Local project identity and shared Markdown routing for Readback 0.7.

The registry is private local configuration. Both host adapters call this module.
Locks are persistent files: projects.lock and locks/<project_uuid>.lock beside it.
Record writers must use these same locks, project first and registry second.
Resolution grants no operating-system permission and never writes records.
"""

import argparse
from contextlib import ExitStack, contextmanager
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
from urllib.parse import quote, unquote_to_bytes
import uuid


MAX_JSON_BYTES = 4 * 1024 * 1024
SLUG = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RECORD_TYPE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
PROJECT_KEYS = {
    "project_uuid", "slug", "home", "home_identity", "home_uuid",
    "protocol_version", "profile_version", "activation_state", "profile_sha256",
    "repositories", "retired_roots",
}
CENTER_KEYS = {
    "project_version", "project_uuid", "home_uuid", "protocol_version",
    "profile_version", "profile_sha256", "activation_state",
}
REPO_KEYS = {
    "repo_uuid", "root", "root_identity", "git_common_dir",
    "git_common_dir_identity", "worktrees",
}
PROFILE_KEYS = {
    "profile_version", "profile_id", "protocol_version", "record_types",
    "core_language", "extension_namespace", "extensions",
}
REPOSITORY_MARKER = ".readback-project.json"


class RegistryError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, message, code="INVALID"):
    if not condition:
        raise RegistryError(code, message)


def _keys(value, expected, label):
    require(type(value) is dict and set(value) == expected,
            label + " has missing or unknown fields")


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _text(value):
    return isinstance(value, str) and bool(value) and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _uuid(value):
    require(isinstance(value, str), "UUID must be text")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise RegistryError("INVALID", "Invalid UUID") from exc
    require(parsed.version == 4 and str(parsed) == value, "Canonical lowercase UUID4 required")
    return value


def _path(value):
    require(_text(value) and Path(value).is_absolute(), "Absolute path required")
    require(os.path.normpath(value) == value, "Normalized path required")
    return Path(value)


def _identity(value):
    _keys(value, {"device", "inode"}, "Filesystem identity")
    require(_integer(value["device"]) and _integer(value["inode"], 1), "Invalid filesystem identity")


def filesystem_identity(path):
    try:
        info = Path(path).stat()
    except (OSError, RuntimeError) as exc:
        raise RegistryError("IDENTITY_MISMATCH", "Bound directory is unavailable") from exc
    require(stat.S_ISDIR(info.st_mode), "Bound path is not a directory", "IDENTITY_MISMATCH")
    return {"device": info.st_dev, "inode": info.st_ino}


def _canonical_directory(value):
    path = _path(value)
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RegistryError("IDENTITY_MISMATCH", "Bound directory is unavailable") from exc
    require(str(canonical) == value and canonical.is_dir(),
            "Registered paths must be canonical directories", "IDENTITY_MISMATCH")
    return canonical


def _check_bound(path, identity):
    _canonical_directory(path)
    require(filesystem_identity(path) == identity, "Bound directory identity changed", "IDENTITY_MISMATCH")


def _secure_components(path, missing_leaf=False):
    """Reject control-path aliases, including symlink ancestors, before opening."""
    path = _path(str(path))
    parts = path.parts
    current = Path(parts[0])
    for index, part in enumerate(parts[1:], 1):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            require(missing_leaf, "Control-path parent does not exist")
            return path
        require(not stat.S_ISLNK(info.st_mode), "Symlink control paths are forbidden")
        if index < len(parts) - 1:
            require(stat.S_ISDIR(info.st_mode), "Control-path parent is not a directory")
    return path


@contextmanager
def _directory_fd(path, create=False):
    """Walk control directories through descriptors, never following an alias."""
    path = _path(str(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise RegistryError("INVALID", "Control-path parent does not exist")
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        _directory_still_bound(path, fd)
        yield fd
    except OSError as exc:
        raise RegistryError("INVALID", "Unsafe or unavailable control directory") from exc
    finally:
        os.close(fd)


def _directory_still_bound(path, fd):
    _secure_components(path)
    actual = Path(path).stat()
    held = os.fstat(fd)
    require((actual.st_dev, actual.st_ino) == (held.st_dev, held.st_ino), "Control directory was replaced")


def _read_bytes_at(path, parent_fd):
    _directory_still_bound(path.parent, parent_fd)
    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                "Control file must be an independent regular file")
        raw = stream.read(MAX_JSON_BYTES + 1)
    _directory_still_bound(path.parent, parent_fd)
    return raw


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def _decode_json(raw):
    require(len(raw) <= MAX_JSON_BYTES, "JSON exceeds the size limit")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_json_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(RegistryError("INVALID", "Non-finite JSON number")))
    except (UnicodeError, ValueError) as exc:
        raise RegistryError("INVALID", "Invalid UTF-8 JSON") from exc


def read_json(path, parent_fd=None):
    path = _secure_components(Path(path))
    try:
        if parent_fd is None:
            with _directory_fd(path.parent) as directory_fd:
                raw = _read_bytes_at(path, directory_fd)
        else:
            raw = _read_bytes_at(path, parent_fd)
    except OSError as exc:
        raise RegistryError("INVALID", "Cannot read control file") from exc
    return _decode_json(raw)


def canonical_json(data):
    try:
        return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RegistryError("INVALID", "Data is not finite JSON") from exc


def _read_profile(path):
    # Hash the same bytes that are parsed; a separate read could bind another file.
    path = _secure_components(Path(path))
    try:
        with _directory_fd(path.parent) as directory_fd:
            raw = _read_bytes_at(path, directory_fd)
    except OSError as exc:
        raise RegistryError("INVALID", "Cannot read profile") from exc
    return _decode_json(raw), hashlib.sha256(raw).hexdigest()


def validate_profile(profile):
    _keys(profile, PROFILE_KEYS, "Profile")
    require(type(profile["profile_version"]) is int and profile["profile_version"] == 1,
            "Unsupported profile version", "UNSUPPORTED_VERSION")
    require(profile["protocol_version"] == "0.7", "Unsupported protocol version", "UNSUPPORTED_VERSION")
    require(isinstance(profile["profile_id"], str) and SLUG.fullmatch(profile["profile_id"]), "Invalid profile ID")
    namespace = profile["extension_namespace"]
    require(isinstance(namespace, str) and SLUG.fullmatch(namespace), "Invalid extension namespace")
    require(profile["core_language"] == "en", "Core language must be English")
    types = profile["record_types"]
    require(type(types) is list and all(isinstance(x, str) and RECORD_TYPE.fullmatch(x) for x in types), "Invalid record types")
    require(len(types) == len(set(types)) and {"DEC", "CON", "HND", "LOG"} <= set(types), "Core record types required without duplicates")
    extensions = profile["extensions"]
    require(type(extensions) is dict and set(extensions) <= {namespace}, "Unknown extension namespace")
    if namespace in extensions:
        require(type(extensions[namespace]) is dict, "Extension content must be an object")
    canonical_json(profile)
    return profile


def _read_repository_marker(path):
    marker = read_json(path)
    _keys(marker, {"marker_version", "project_uuid", "repo_uuid"}, "Repository marker")
    require(type(marker["marker_version"]) is int and marker["marker_version"] == 1,
            "Unsupported repository marker version", "UNSUPPORTED_VERSION")
    _uuid(marker["project_uuid"])
    _uuid(marker["repo_uuid"])
    return marker


def _git_identity(directory):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(directory),
             "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"],
            capture_output=True, text=True, timeout=10, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RegistryError("INVALID", "Git identity could not be read") from exc
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    require(len(lines) == 2 and all(_text(x) for x in lines), "Ambiguous Git identity")
    try:
        return str(Path(lines[0]).resolve(strict=True)), str(Path(lines[1]).resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        raise RegistryError("IDENTITY_MISMATCH", "Git identity paths are unavailable") from exc


def observe_repository(root, repo_uuid=None, worktrees=()):
    """Observe explicitly supplied roots. Never discover or register a worktree silently."""
    try:
        root = str(Path(root).resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        raise RegistryError("INVALID", "Repository root is unavailable") from exc
    actual = _git_identity(root)
    require(actual is not None and actual[0] == root, "Root must be a Git worktree root")
    common = actual[1]
    observed = []
    for tree in worktrees:
        try:
            tree = str(Path(tree).resolve(strict=True))
        except (OSError, RuntimeError) as exc:
            raise RegistryError("INVALID", "Worktree root is unavailable") from exc
        require(tree != root and _git_identity(tree) == (tree, common), "Worktree belongs to another repository")
        observed.append({"root": tree, "identity": filesystem_identity(tree)})
    return {"repo_uuid": _uuid(repo_uuid) if repo_uuid else str(uuid.uuid4()),
            "root": root, "root_identity": filesystem_identity(root),
            "git_common_dir": common, "git_common_dir_identity": filesystem_identity(common),
            "worktrees": observed}


def _overlap(left, right):
    left, right = Path(left), Path(right)
    return left == right or left in right.parents or right in left.parents


def validate_registry(data, check_filesystem=True):
    _keys(data, {"registry_version", "generation", "projects"}, "Registry")
    require(type(data["registry_version"]) is int and data["registry_version"] == 1,
            "Unsupported registry version", "UNSUPPORTED_VERSION")
    require(_integer(data["generation"]), "Invalid registry generation")
    require(type(data["projects"]) is list, "Projects must be a list")
    project_ids, home_ids, repo_ids, paths, commons, reservations = set(), set(), set(), {}, {}, []
    physical_paths, physical_commons, physical_homes = {}, {}, {}
    for project in data["projects"]:
        _keys(project, PROJECT_KEYS, "Project")
        pid, hid = _uuid(project["project_uuid"]), _uuid(project["home_uuid"])
        require(pid not in project_ids and hid not in home_ids, "Duplicate project or home UUID")
        project_ids.add(pid); home_ids.add(hid)
        require(isinstance(project["slug"], str) and SLUG.fullmatch(project["slug"]), "Invalid project slug")
        require(project["protocol_version"] == "0.7" and type(project["profile_version"]) is int and project["profile_version"] == 1,
                "Unsupported project version", "UNSUPPORTED_VERSION")
        require(project["activation_state"] in ("STAGED", "ACTIVE"), "Unsupported activation state", "INACTIVE")
        require(isinstance(project["profile_sha256"], str) and SHA256.fullmatch(project["profile_sha256"]), "Invalid profile hash")
        home = str(_path(project["home"])); _identity(project["home_identity"])
        require(type(project["repositories"]) is list and bool(project["repositories"]), "At least one explicit repository is required")
        require(type(project["retired_roots"]) is list, "Retired roots must be a list")
        reserved = [home] + [str(_path(x)) for x in project["retired_roots"]]
        require(len(reserved) == len(set(reserved)), "Duplicate reserved path")
        for path in reserved:
            require(not any(_overlap(path, prior) for prior, _ in reservations), "Reserved paths overlap")
            reservations.append((path, pid))
        if check_filesystem:
            _check_bound(home, project["home_identity"])
            physical = tuple(project["home_identity"][key] for key in ("device", "inode"))
            require(physical not in physical_homes, "Same physical home is bound twice")
            physical_homes[physical] = home
            center = read_json(Path(home) / "project.json")
            _keys(center, CENTER_KEYS, "Center identity")
            require(type(center["project_version"]) is int and center["project_version"] == 1, "Unsupported center version", "UNSUPPORTED_VERSION")
            for key in CENTER_KEYS - {"project_version"}:
                require(center[key] == project[key] and type(center[key]) is type(project[key]), "Center identity differs from registry", "IDENTITY_MISMATCH")
            profile, digest = _read_profile(Path(home) / "profile.json")
            validate_profile(profile)
            require(digest == project["profile_sha256"], "Profile content differs from registry", "IDENTITY_MISMATCH")
            if project["activation_state"] == "ACTIVE":
                extension = profile["extensions"].get(profile["extension_namespace"], {})
                require(extension.get("storage") == "markdown-single-writer",
                        "Active projects require explicit markdown-single-writer storage", "INACTIVE")
            for path in project["retired_roots"]:
                _canonical_directory(path)
        for repo in project["repositories"]:
            _keys(repo, REPO_KEYS, "Repository")
            rid = _uuid(repo["repo_uuid"])
            require(rid not in repo_ids, "Duplicate repository UUID"); repo_ids.add(rid)
            root = str(_path(repo["root"])); common = str(_path(repo["git_common_dir"]))
            _identity(repo["root_identity"]); _identity(repo["git_common_dir_identity"])
            require(common not in commons, "Git common directory is registered twice")
            commons[common] = rid
            require(type(repo["worktrees"]) is list, "Worktrees must be a list")
            bindings = [(root, repo["root_identity"])]
            for tree in repo["worktrees"]:
                _keys(tree, {"root", "identity"}, "Worktree")
                _identity(tree["identity"])
                bindings.append((str(_path(tree["root"])), tree["identity"]))
            for path, identity in bindings:
                require(path not in paths, "Repository root is bound twice")
                paths[path] = pid
                if check_filesystem:
                    _check_bound(path, identity)
                    physical = (identity["device"], identity["inode"])
                    require(physical not in physical_paths, "Same physical root is bound twice")
                    physical_paths[physical] = path
                    require(_git_identity(path) == (path, common), "Git root or common directory changed", "IDENTITY_MISMATCH")
                    marker_path = Path(path) / REPOSITORY_MARKER
                    marker_exists = marker_path.exists() or marker_path.is_symlink()
                    require(marker_exists or project["activation_state"] != "ACTIVE" or path in project["retired_roots"],
                            "Active repository has no project marker", "IDENTITY_MISMATCH")
                    if marker_exists:
                        marker = _read_repository_marker(marker_path)
                        require(marker["project_uuid"] == pid and marker["repo_uuid"] == rid,
                                "Repository marker differs from registry", "IDENTITY_MISMATCH")
            if check_filesystem:
                _check_bound(common, repo["git_common_dir_identity"])
                physical = tuple(repo["git_common_dir_identity"][key] for key in ("device", "inode"))
                require(physical not in physical_commons, "Same physical Git common directory is bound twice")
                physical_commons[physical] = common
    require(not (set(physical_homes) & set(physical_paths)), "Home and repository use the same physical root")
    for path, owner in paths.items():
        for reserved, pid in reservations:
            # A former Hub may be both a registered repository and a retired root.
            require(not _overlap(path, reserved) or (path == reserved and owner == pid and
                    any(reserved in p["retired_roots"] for p in data["projects"] if p["project_uuid"] == pid)),
                    "Repository and reserved center paths overlap")
    canonical_json(data)
    return data


def _registry_path(path):
    path = Path(path)
    require(path.is_absolute(), "Registry path must be absolute")
    _secure_components(path, missing_leaf=True)
    require(path.name not in {"projects.lock", "locks"}, "Registry path collides with a lock path")
    return path


@contextmanager
def _lock(path, deadline):
    _secure_components(path, missing_leaf=True)
    with _directory_fd(path.parent) as parent_fd:
        fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "Lock must be an independent regular file")
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic() < deadline, "Registry lock deadline exceeded", "BUSY")
                    time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            require((current.st_dev, current.st_ino) == (info.st_dev, info.st_ino) and not stat.S_ISLNK(current.st_mode),
                    "Persistent lock file was replaced")
            _directory_still_bound(path.parent, parent_fd)
            yield parent_fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _atomic_write(path, data, parent_fd):
    _secure_components(path, missing_leaf=True)
    raw = canonical_json(data)
    require(len(raw) <= MAX_JSON_BYTES, "Registry exceeds the size limit")
    _directory_still_bound(path.parent, parent_fd)
    temporary = ".registry-" + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        _secure_components(path, missing_leaf=True)
        _directory_still_bound(path.parent, parent_fd)
        os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def init_registry(path, lock_timeout=5):
    path = _registry_path(path)
    require(type(lock_timeout) in (int, float) and 0 < lock_timeout <= 60, "Invalid lock timeout")
    with _directory_fd(path.parent, create=True):
        with _lock(path.parent / "projects.lock", time.monotonic() + lock_timeout) as parent_fd:
            require(not path.exists(), "Registry already exists", "CONFLICT")
            result = {"registry_version": 1, "generation": 0, "projects": []}
            _atomic_write(path, result, parent_fd)
    return result


def _validate_update(previous, candidate):
    old = {p["project_uuid"]: p for p in previous["projects"]}
    new = {p["project_uuid"]: p for p in candidate["projects"]}
    require(set(old) <= set(new), "Project identities cannot be removed")
    for pid, prior in old.items():
        future = new[pid]
        require(prior["home_uuid"] == future["home_uuid"] and prior["home"] == future["home"],
                "Project home identity cannot be replaced")
        require(set(prior["retired_roots"]) <= set(future["retired_roots"]), "Reserved roots cannot be released")
        old_repos = {repo["repo_uuid"] for repo in prior["repositories"]}
        new_repos = {repo["repo_uuid"] for repo in future["repositories"]}
        require(old_repos <= new_repos, "Rebinding must retain repository UUIDs")


def _require_finished_publications(project):
    """A generation update must not strand an interrupted Markdown publication."""
    folder = Path(project["home"]) / ".readback" / "publications"
    try:
        _secure_components(folder, missing_leaf=True)
        if not folder.exists():
            return
        with _directory_fd(folder) as parent_fd:
            for name in sorted(os.listdir(parent_fd)):
                if not name.endswith(".json"):
                    continue
                operation_id = _uuid(name[:-5])
                journal = read_json(folder / name, parent_fd)
                _keys(journal, {"schema_version", "state", "request_sha256", "request", "project_uuid",
                                "registry_generation", "previous_current", "previous_log_sha256", "log_after_sha256",
                                "log_suffix", "provenance", "record_hashes"}, "Committed publication journal")
                require(type(journal["schema_version"]) is int and journal["schema_version"] == 1,
                        "Unsupported publication journal schema")
                require(journal["state"] == "COMMITTED", "Publication is unfinished")
                require(journal["project_uuid"] == project["project_uuid"], "Publication belongs to another project")
                require(_integer(journal["registry_generation"]), "Invalid publication generation")
                request = journal["request"]
                _keys(request, {"operation_id", "expected_generation", "expected_current_sha256", "records", "log_lines", "current"},
                      "Publication request")
                require(request["operation_id"] == operation_id and _integer(request["expected_generation"]) and
                        request["expected_generation"] == journal["registry_generation"],
                        "Publication identity differs from its journal")
                require(journal["request_sha256"] == hashlib.sha256(canonical_json(request)).hexdigest(),
                        "Publication request hash differs")
                for key in ("expected_current_sha256",):
                    require(request[key] is None or isinstance(request[key], str) and SHA256.fullmatch(request[key]),
                            "Invalid publication CURRENT hash")
                require(type(request["records"]) is list and type(request["log_lines"]) is list and
                        isinstance(request["current"], str), "Invalid publication request fields")
                for key in ("previous_current",):
                    require(journal[key] is None or isinstance(journal[key], str), "Invalid previous CURRENT")
                require(journal["previous_log_sha256"] is None or isinstance(journal["previous_log_sha256"], str) and
                        SHA256.fullmatch(journal["previous_log_sha256"]), "Invalid previous LOG hash")
                require(isinstance(journal["log_after_sha256"], str) and SHA256.fullmatch(journal["log_after_sha256"]),
                        "Invalid publication LOG hash")
                require(isinstance(journal["log_suffix"], str), "Invalid publication LOG suffix")
                _keys(journal["provenance"], {"repo_uuid", "cwd", "worktree_root", "git_common_dir"}, "Publication provenance")
                hashes = journal["record_hashes"]
                require(type(hashes) is list and all(isinstance(value, str) and
                        re.fullmatch(r"(?:CON|HND|DEC|NOTE)-\d{8}-\d{2,6}@sha256:[0-9a-f]{64}", value) for value in hashes),
                        "Invalid publication record hashes")
    except (RegistryError, OSError, ValueError) as exc:
        raise RegistryError("RECOVERY_REQUIRED", "Finish or repair publication journals before changing registry generation") from exc


def apply_registry(path, candidate, expected_generation, lock_timeout=5):
    path = _registry_path(path)
    require(_integer(expected_generation), "Invalid expected generation")
    require(type(lock_timeout) in (int, float) and 0 < lock_timeout <= 60, "Invalid lock timeout")
    candidate = _decode_json(canonical_json(candidate))
    validate_registry(candidate)
    require(candidate["generation"] == expected_generation, "Candidate generation differs from expected generation", "CONFLICT")
    deadline = time.monotonic() + lock_timeout
    lock_dir = path.parent / "locks"
    with _directory_fd(lock_dir, create=True):
        pass
    while True:
        before = validate_registry(read_json(path), check_filesystem=False)
        require(before["generation"] == expected_generation, "Registry generation changed", "CONFLICT")
        affected = sorted({p["project_uuid"] for p in before["projects"] + candidate["projects"]})
        with ExitStack() as locks:
            for pid in affected:
                locks.enter_context(_lock(lock_dir / (pid + ".lock"), deadline))
            registry_fd = locks.enter_context(_lock(path.parent / "projects.lock", deadline))
            current = validate_registry(read_json(path, registry_fd), check_filesystem=False)
            require(current["generation"] == expected_generation, "Registry generation changed", "CONFLICT")
            current_affected = sorted({p["project_uuid"] for p in current["projects"] + candidate["projects"]})
            if current_affected != affected or canonical_json(before) != canonical_json(current):
                require(time.monotonic() < deadline, "Registry changed during lock acquisition", "BUSY")
                continue  # ExitStack releases every lock before retrying.
            _validate_update(current, candidate)
            validate_registry(candidate)
            for project in current["projects"]:
                _require_finished_publications(project)
            published = dict(candidate, generation=expected_generation + 1)
            _atomic_write(path, published, registry_fd)
            return published


def _provenance(cwd, git):
    """Return the exact worktree context; a project home is not a code checkout."""
    return {"cwd": str(cwd), "worktree_root": git[0] if git else None,
            "git_common_dir": git[1] if git else None}


def _resolution(project, registry, host, cwd, git, repo_uuid=None):
    home = Path(project["home"])
    return {"status": project["activation_state"], "host": host, "project_uuid": project["project_uuid"],
            "project_slug": project["slug"],
            "home_uuid": project["home_uuid"], "home": project["home"],
            "protocol_version": project["protocol_version"], "profile_version": project["profile_version"],
            "profile_path": str(home / "profile.json"), "log_path": str(home / "LOG.md"),
            "current_path": str(home / "CURRENT.md"), "records_path": str(home / "records"),
            "registry_generation": registry["generation"], "repo_uuid": repo_uuid,
            "write_allowed": project["activation_state"] == "ACTIVE", **_provenance(cwd, git)}


def _linked_main_name(directory):
    """Name of the main worktree's folder for a linked worktree whose main root counts, else None."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    prefix = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(directory)]
    try:
        ident = subprocess.run(prefix + ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
                               capture_output=True, timeout=5, env=env, check=False)
        if ident.returncode != 0:
            return None
        lines = ident.stdout.split(b"\n")
        if len(lines) != 3 or lines[2] != b"":
            return None
        if os.path.realpath(os.fsdecode(lines[0])) == os.path.realpath(os.fsdecode(lines[1])):
            return None
        listing = subprocess.run(prefix + ["worktree", "list", "--porcelain", "-z"],
                                 capture_output=True, timeout=5, env=env, check=False)
        if listing.returncode != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    record = listing.stdout.split(b"\0\0")[0].split(b"\0")
    if not record or not record[0].startswith(b"worktree ") or b"bare" in record[1:]:
        return None
    main = os.fsdecode(record[0][len(b"worktree "):])
    if not main or not os.path.exists(os.path.join(main, ".git")):
        return None
    return Path(main).name or None


def resolve_project(registry_path, cwd, host, inspect_staged=False, logbook=None, legacy_fallback=False):
    require(host in {"codex", "claude"}, "Unsupported host")
    require(type(inspect_staged) is bool, "Inspection flag must be boolean")
    require(type(legacy_fallback) is bool, "Fallback flag must be boolean")
    path = _registry_path(registry_path)
    if path.exists():
        registry = validate_registry(read_json(path))
    else:
        require(legacy_fallback, "Registry is missing; request explicit legacy fallback", "NOT_FOUND")
        # Compatibility reads create neither a registry nor its parent directories.
        registry = {"registry_version": 1, "generation": None, "projects": []}
    requested_cwd = Path(os.path.abspath(cwd))
    for project in registry["projects"]:
        for retired in project["retired_roots"]:
            require(not (requested_cwd == Path(retired) or Path(retired) in requested_cwd.parents),
                    "Retired root; use project home " + project["home"], "RETIRED")
    try:
        cwd = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RegistryError("INVALID", "Working directory is unavailable") from exc
    require(cwd.is_dir(), "Working directory must be a directory")
    git = _git_identity(cwd)
    # Explicit centers and retired roots take priority over any parent Git repo.
    for project in registry["projects"]:
        for retired in project["retired_roots"]:
            require(not (cwd == Path(retired) or Path(retired) in cwd.parents),
                    "Retired root; use project home " + project["home"], "RETIRED")
        for reserved in [project["home"]]:
            if cwd == Path(reserved) or Path(reserved) in cwd.parents:
                require(project["activation_state"] == "ACTIVE" or inspect_staged,
                        "Project is staged; writes are disabled", "INACTIVE")
                return _resolution(project, registry, host, cwd, None)
    if git is not None:
        root, common = git
        for project in registry["projects"]:
            for repo in project["repositories"]:
                known = [repo["root"]] + [tree["root"] for tree in repo["worktrees"]]
                if root in known:
                    require(common == repo["git_common_dir"], "Git common directory changed", "IDENTITY_MISMATCH")
                    require(project["activation_state"] == "ACTIVE" or inspect_staged,
                            "Project is staged; writes are disabled", "INACTIVE")
                    return _resolution(project, registry, host, cwd, git, repo["repo_uuid"])
                require(common != repo["git_common_dir"], "Worktree has no explicit binding", "UNREGISTERED_WORKTREE")
    for parent in [cwd] + list(cwd.parents):
        repository_marker = parent / REPOSITORY_MARKER
        if repository_marker.exists() or repository_marker.is_symlink():
            _read_repository_marker(repository_marker)
            raise RegistryError("UNREGISTERED_REPOSITORY", "Marked repository has no matching registered location; rebind explicitly")
        marker = parent / "project.json"
        if marker.exists() or marker.is_symlink():
            # A copied/unknown center may not escape to the basename fallback.
            value = read_json(marker)
            if type(value) is dict and ("project_uuid" in value or "project_version" in value):
                raise RegistryError("UNREGISTERED_CENTER", "Center has no matching registered location")
        if parent == Path(parent.anchor):
            break
    require(legacy_fallback, "Repository has no explicit binding", "UNKNOWN_REPOSITORY")
    root = Path(git[0]) if git else cwd
    try:
        base = Path(logbook).resolve() if logbook is not None else Path.home() / "logbook"
        name = (_linked_main_name(cwd) if git else None) or root.name
        destination = (base / name).resolve()
    except (OSError, RuntimeError) as exc:
        raise RegistryError("INVALID", "Fallback path is unavailable") from exc
    for parent in [destination] + list(destination.parents):
        marker = parent / "project.json"
        if marker.exists() or marker.is_symlink():
            value = read_json(marker)
            require(not (type(value) is dict and ("project_uuid" in value or "project_version" in value)),
                    "Legacy fallback points into an unregistered center", "UNREGISTERED_CENTER")
    for project in registry["projects"]:
        for reserved in [project["home"]] + project["retired_roots"]:
            require(not _overlap(destination, reserved), "Legacy fallback collides with a reserved path", "RESERVED_PATH")
    return {"status": "LEGACY_FALLBACK", "host": host, "home": str(destination),
            "protocol_version": "0.7", "registry_generation": registry["generation"],
            "project_uuid": None, "repo_uuid": None, "write_allowed": False,
            "log_path": str(destination / "LOG.md"), "current_path": str(destination / "CURRENT.md"),
            "records_path": str(destination / "records"), **_provenance(cwd, git)}


def make_legacy_ref(source_uuid, snapshot_sha256, path, raw_id):
    _uuid(source_uuid)
    require(isinstance(snapshot_sha256, str) and SHA256.fullmatch(snapshot_sha256), "Invalid snapshot hash")
    require(_text(path) and not path.startswith("/") and all(x not in {"", ".", ".."} for x in path.split("/")), "Invalid legacy path")
    require(_text(raw_id), "Invalid legacy ID")
    return "legacy:" + source_uuid + ":" + snapshot_sha256 + ":" + quote(path, safe="/") + "#" + quote(raw_id, safe="")


def parse_legacy_ref(reference):
    require(isinstance(reference, str) and reference.startswith("legacy:"), "Qualified legacy reference required")
    try:
        _, source, snapshot, tail = reference.split(":", 3)
        path, raw_id = tail.split("#", 1)
        path = unquote_to_bytes(path).decode("utf-8")
        raw_id = unquote_to_bytes(raw_id).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise RegistryError("INVALID", "Invalid legacy reference") from exc
    require(make_legacy_ref(source, snapshot, path, raw_id) == reference, "Legacy reference is not canonical")
    return {"source_uuid": source, "snapshot_sha256": snapshot, "path": path, "raw_id": raw_id}


def resolve_legacy_ref(reference, entries, source_uuid=None):
    require(type(entries) is list and _text(reference), "Invalid legacy lookup")
    if source_uuid is not None:
        _uuid(source_uuid)
    qualified = reference.startswith("legacy:")
    if qualified:
        parse_legacy_ref(reference)
    seen, matches = set(), []
    for entry in entries:
        _keys(entry, {"reference", "target"}, "Legacy mapping")
        parsed = parse_legacy_ref(entry["reference"])
        require(entry["reference"] not in seen, "Duplicate qualified legacy reference")
        require(_text(entry["target"]), "Invalid legacy target")
        seen.add(entry["reference"])
        if source_uuid is not None and parsed["source_uuid"] != source_uuid:
            continue
        if (qualified and entry["reference"] == reference) or (not qualified and parsed["raw_id"] == reference):
            matches.append(entry["target"])
    require(matches, "Legacy reference has no target", "NOT_FOUND")
    require(len(matches) == 1, "Legacy alias has multiple targets", "AMBIGUOUS_REFERENCE")
    return matches[0]


def main(argv=None, fixed_host=None):
    parser = argparse.ArgumentParser(description=__doc__)
    if fixed_host:
        parser.add_argument("--registry", required=True)
        parser.add_argument("--cwd", default=os.getcwd())
        parser.add_argument("--inspect-staged", action="store_true")
        parser.add_argument("--logbook")
        parser.add_argument("--legacy-fallback", action="store_true")
    else:
        sub = parser.add_subparsers(dest="command", required=True)
        for name in ["init", "validate", "apply", "resolve"]:
            child = sub.add_parser(name)
            child.add_argument("--registry", required=True)
            if name in {"init", "apply"}:
                child.add_argument("--lock-timeout", type=float, default=5)
            if name == "apply":
                child.add_argument("--candidate", required=True)
                child.add_argument("--expected-generation", type=int, required=True)
            if name == "resolve":
                child.add_argument("--cwd", default=os.getcwd())
                child.add_argument("--host", choices=["codex", "claude"], required=True)
                child.add_argument("--inspect-staged", action="store_true")
                child.add_argument("--logbook")
                child.add_argument("--legacy-fallback", action="store_true")
    args = parser.parse_args(argv)
    try:
        if fixed_host or args.command == "resolve":
            result = resolve_project(args.registry, args.cwd, fixed_host or args.host, args.inspect_staged, args.logbook, args.legacy_fallback)
        elif args.command == "init":
            result = init_registry(args.registry, args.lock_timeout)
        elif args.command == "apply":
            result = apply_registry(args.registry, read_json(args.candidate), args.expected_generation, args.lock_timeout)
        else:
            result = validate_registry(read_json(args.registry))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (RegistryError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, RegistryError) else "INVALID"
        print(json.dumps({"status": code, "error": str(exc), "write_allowed": False}), file=sys.stderr)
        return 3 if code == "BUSY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
