"""Read project-associated provider metadata. No conversation content is used."""
from collections import namedtuple
from contextlib import closing
import fcntl
from functools import lru_cache
from itertools import islice
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import uuid

import project_registry as registry


SCAN_LIMIT = 256
DISPLAY_LIMIT = 24
Candidate = namedtuple("Candidate", "id cwd timestamp witness")


def valid_id(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def metadata_files(folder, pattern):
    if not folder.exists():
        return [], False
    files = list(islice((file for file in folder.iterdir() if re.fullmatch(pattern, file.name)), SCAN_LIMIT + 1))
    return files[:SCAN_LIMIT], len(files) > SCAN_LIMIT


def claude_candidates(config, matches):
    files, truncated = metadata_files(config / "sessions", r"[1-9][0-9]{0,9}\.json")
    rows = []
    unreadable = False
    for path in files:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
                unreadable = True
                continue
            with path.open("rb") as handle:
                data = json.loads(handle.read(64 * 1024 + 1))
        except FileNotFoundError:  # The process finished during enumeration.
            continue
        except (OSError, ValueError):
            unreadable = True
            continue
        if not isinstance(data, dict) or not valid_id(data.get("sessionId")) or not isinstance(data.get("cwd"), str):
            unreadable = True
            continue
        pid = int(path.stem)
        witness = (pid, data.get("procStart")) if data.get("pid") == pid else None
        rows.append(Candidate(data["sessionId"], data.get("cwd"), info.st_mtime, witness))
    return rows, truncated, unreadable


def claude_running(witness):
    if not witness or not isinstance(witness[1], str) or not witness[1]:
        return False
    pid, started = witness
    try:
        # Claude's procStart uses English UTC. Comparing birth time rejects recycled PIDs.
        proc = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                              text=True, timeout=1, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.split() == started.split()


def codex_running(lock):
    if lock is None:
        return False
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        # Codex retains empty files after release. Only an exclusive writer
        # conflicts with this shared probe. Closing the fd releases our probe.
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def codex_database(config):
    if not config.exists():
        return None
    databases = [(int(match[1]), p) for p in config.iterdir()
                 if (match := re.fullmatch(r"state_(\d+)\.sqlite", p.name)) and not p.is_symlink()]
    if not databases:
        return None
    database = max(databases)[1]
    # Read the current WAL when present. A closed database can be read without creating sidecars.
    uri = database.as_uri() + "?mode=ro"
    if not Path(str(database) + "-wal").exists():
        uri += "&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=0.25)


def codex_candidates(config, matches):
    connection = codex_database(config)
    if connection is None:
        return [], False, False
    with closing(connection) as db:
        files, truncated = metadata_files(config / "thread-writer-locks", r"[0-9a-fA-F-]{36}\.lock")
        locks = {p.stem: p for p in files if valid_id(p.stem)}
        columns = "SELECT id,cwd,updated_at FROM threads WHERE archived=0"
        # Resolve each distinct working directory once, before limiting the
        # selected project's history. Busy threads in another project must not
        # crowd out every row of a quiet project.
        directories = db.execute("SELECT cwd FROM threads WHERE archived=0 GROUP BY cwd "
                                 "ORDER BY MAX(updated_at) DESC LIMIT ?", (SCAN_LIMIT + 1,)).fetchall()
        truncated |= len(directories) > SCAN_LIMIT
        selected = [cwd for (cwd,) in directories[:SCAN_LIMIT] if isinstance(cwd, str) and matches(cwd)]
        rows = []
        if selected:
            slots = ",".join("?" for _ in selected)
            rows = db.execute(columns + " AND cwd IN (" + slots + ") ORDER BY updated_at DESC LIMIT ?",
                              (*selected, DISPLAY_LIMIT + 1)).fetchall()
        if locks:
            # Include long-running sessions even if newer history fills the recent window.
            slots = ",".join("?" for _ in locks)
            rows += db.execute(columns + " AND id IN (" + slots + ")", tuple(locks)).fetchall()
    unique = {key: Candidate(key, cwd, updated, locks.get(key)) for key, cwd, updated in rows
              if valid_id(key) and type(updated) in (int, float)}
    return list(unique.values()), truncated, False


def project_matcher(project, binding, book):
    home = Path(project["location"])
    registry_path = book / ".readback/projects.json"
    roots = [home]
    if binding:
        for entry in registry.read_json(registry_path)["projects"]:
            if entry["project_uuid"] == binding[0]:
                for repo in entry["repositories"]:
                    roots.append(Path(repo["root"]))
                    roots.extend(Path(tree["root"]) for tree in repo["worktrees"])

    @lru_cache(maxsize=SCAN_LIMIT * 2)
    def matches(cwd):
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return False
        path = Path(cwd)
        if binding:
            if not any(path == root or root in path.parents for root in roots):
                return False
        elif home.name not in path.parts:
            # Linked worktrees can live anywhere under names unrelated to the main checkout.
            if not any((parent / ".git").is_file() for parent in (path, *path.parents)):
                return False
        try:
            resolved = registry.resolve_project(registry_path, path, "codex", logbook=book, legacy_fallback=True)
        except registry.RegistryError:
            return False
        return resolved["home"] == str(home) and resolved["project_uuid"] == (binding[0] if binding else None)
    return matches


def for_project(project, binding, book):
    matches = project_matcher(project, binding, book)
    providers = [
        ("claude", Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")), claude_candidates, claude_running),
        ("codex", Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")), codex_candidates, codex_running),
    ]
    observed = {}
    truncated = False
    unavailable = []
    for host, directory, candidates, running in providers:
        try:
            rows, limited, unreadable = candidates(directory, matches)
            if unreadable:
                unavailable.append(host)
            truncated |= limited
            for row in rows:
                if isinstance(row.cwd, str) and matches(row.cwd):
                    key = host, row.id
                    result = running(row.witness), row.timestamp
                    observed[key] = max(observed.get(key, result), result)
        except (OSError, sqlite3.Error, ValueError):
            unavailable.append(host)
    ordered = sorted(observed, key=observed.get, reverse=True)
    sessions = [{"id": key[1], "host": key[0], "status": "running" if observed[key][0] else "recorded"}
                for key in ordered[:DISPLAY_LIMIT]]
    return {"sessions": sessions, "truncated": truncated or len(ordered) > DISPLAY_LIMIT,
            "unavailable": unavailable}
