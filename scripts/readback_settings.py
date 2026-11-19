#!/usr/bin/env python3
"""Readback settings: one local file per project home, read and written here only.

The file is <home>/settings.json. It is local: the agent runs as the same user and
can write it. The digest detects a difference; it does not show who wrote the file.
Commands: show, set <dotted key> <value>, migrate-teammates [--table PATH] [--write].
"""

import argparse
from contextlib import contextmanager, suppress
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

sys.dont_write_bytecode = True  # the sibling imports below must leave no .pyc file next to the command
import outside_judge  # noqa: E402
import project_registry  # noqa: E402

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
_EFFORT = re.compile(r"[A-Za-z0-9_-]+")
DEFAULTS = {
    "schema_version": 1,
    "judge": {
        "mode": "gate",
        "provider": "opposite",
        "routes": {
            "claude": {"step": {"model": "fable", "effort": "high"}, "gate": {"model": "fable", "effort": "high"}},
            "codex": {"step": {"model": "gpt-6-astra", "effort": "high"},
                  "gate": {"model": "gpt-6-astra", "effort": "high"}},
        },
        "deadline_seconds": {"step": 300, "gate": 900},
        "max_fail_rounds": 3,
    },
    "commit_check": "observe",
}
# One rule for each leaf name. A name that holds an object in DEFAULTS never reaches this table.
_RULES = {
    "mode": lambda v: v in ("off", "gate", "each"),
    "provider": lambda v: v == "opposite",
    "model": lambda v: type(v) is str and _MODEL.fullmatch(v) is not None,
    "effort": lambda v: type(v) is str and _EFFORT.fullmatch(v) is not None,
    "step": lambda v: type(v) is int and 30 <= v <= 3600,
    "gate": lambda v: type(v) is int and 30 <= v <= 3600,
    "max_fail_rounds": lambda v: type(v) is int and 1 <= v <= 10,
    "commit_check": lambda v: v in ("off", "observe"),
}


class SettingsError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def leaves(shape=None, trail=""):
    """Map each dotted leaf key of the schema to its default."""
    found = {}
    for key, value in (DEFAULTS if shape is None else shape).items():
        if type(value) is dict:
            found.update(leaves(value, trail + key + "."))
        elif key != "schema_version":  # the file version is not a setting
            found[trail + key] = value
    return found


def canonical(settings):
    return json.dumps(settings, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(settings):
    return hashlib.sha256(canonical(settings)).hexdigest()


def _bad(path, dotted, reason):
    return SettingsError("INVALID_SETTINGS", f"{path}: {dotted}: {reason}")


def _walk(shape, node, trail, path):
    for key, value in node.items():
        if key not in shape:
            raise _bad(path, trail + key, "unknown key")
        if type(shape[key]) is dict:
            if type(value) is not dict:
                raise _bad(path, trail + key, "must be an object")
            _walk(shape[key], value, trail + key + ".", path)
        elif not _RULES[key](value):
            raise _bad(path, trail + key, "invalid value")


def check(obj, path):
    """Validate one whole settings file object. The path only names the file in messages."""
    if type(obj) is not dict:
        raise _bad(path, "$", "must be an object")
    if "schema_version" not in obj:
        raise _bad(path, "schema_version", "is required")
    if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:
        raise _bad(path, "schema_version", "must be 1")
    _walk(DEFAULTS, {k: v for k, v in obj.items() if k != "schema_version"}, "", path)


def _pairs(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _refuse(name):
    raise ValueError("constant " + name)


def _read(home):
    """The checked file object, or None when there is no file."""
    path = Path(home) / "settings.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SettingsError("INVALID_SETTINGS", f"{path}: cannot read the file: {exc.strerror}") from exc
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_refuse)
    except (ValueError, RecursionError) as exc:
        raise SettingsError("INVALID_SETTINGS", f"{path}: not valid JSON: {exc}") from exc
    check(obj, str(path))
    return obj


def _fill(base, raw):
    return {k: _fill(v, raw.get(k, {})) if type(v) is dict else raw.get(k, v) for k, v in base.items()}


def _legacy_routes(table):
    """Explicit provider routes applied by outside_judge before a project settings file exists."""
    routes = {}
    for host in ("claude", "codex"):
        try:
            route = outside_judge.select_route(host, Path(table))
        except outside_judge.JudgeError as exc:
            raise SettingsError("INVALID_TABLE", f"{table}: {exc}") from exc
        if route.source == "table":
            routes[route.provider] = {tier: {"model": route.model, "effort": route.effort}
                                      for tier in ("step", "gate")}
    return routes


def _initial_settings(legacy_table):
    obj = {"schema_version": 1}
    if legacy_table is not None:
        routes = _legacy_routes(legacy_table)
        if routes:
            obj["judge"] = {"routes": routes}
    return obj


def load(home, *, legacy_table=None):
    obj = _read(home)
    settings = _fill(DEFAULTS, _initial_settings(legacy_table) if obj is None else obj)
    return {"settings": settings, "source": "defaults" if obj is None else "file",
            "path": str(Path(home) / "settings.json"), "digest": digest(settings)}


@contextmanager
def _locked(home):
    if not os.path.isdir(home):
        raise SettingsError("HOME_MISSING", f"{home}: the project home folder does not exist")
    try:
        fd = os.open(Path(home) / ".settings.lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise SettingsError("WRITE_FAILED", f"{home}/.settings.lock: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _publish(home, obj):
    """Temporary file, mode 0600 from mkstemp, sync, rename, sync of the folder."""
    target = Path(home) / "settings.json"
    data = (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    temp = None
    try:
        fd, temp = tempfile.mkstemp(dir=home, prefix=".settings.", suffix=".tmp")
        try:
            if os.write(fd, data) != len(data):
                raise OSError("short write")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, target)
        folder = os.open(home, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    except OSError as exc:
        if temp is not None:
            with suppress(FileNotFoundError):
                os.unlink(temp)
        raise SettingsError("WRITE_FAILED", f"{target}: {exc}") from exc


def update(home, key, value, *, legacy_table=None):
    path = str(Path(home) / "settings.json")
    with _locked(home):
        obj = _read(home)
        if obj is None:
            obj = _initial_settings(legacy_table)
        *parents, leaf = key.split(".")
        node = obj
        for part in parents:
            node = node.setdefault(part, {})
            if type(node) is not dict:
                raise _bad(path, key, "cannot set below a value")
        node[leaf] = value
        check(obj, path)
        _publish(home, obj)
        return load(home)


def resolve(cwd, host, registry=None, logbook=None):
    book = Path.home() / "logbook"
    try:
        found = project_registry.resolve_project(
            book / ".readback" / "projects.json" if registry is None else registry,
            cwd, host, logbook=book if logbook is None else logbook, legacy_fallback=True)
    except project_registry.RegistryError as exc:
        raise SettingsError(exc.code, str(exc)) from exc
    if found["status"] not in ("ACTIVE", "LEGACY_FALLBACK"):
        raise SettingsError(found["status"], "the project is not usable: " + found["status"])
    return {"status": found["status"], "home": found["home"], "project_uuid": found["project_uuid"],
            "repo_uuid": found["repo_uuid"], "worktree_root": found["worktree_root"],
            "git_common_dir": found["git_common_dir"],
            "repository": found["repo_uuid"] or found["git_common_dir"] or str(Path(cwd).resolve())}


def migrate_teammates(home, table, write=False):
    """Map the legacy judge table to settings leaves; with write, fill only the leaves that are missing."""
    mapping = {f"judge.routes.{provider}.{tier}.{leaf}": value
               for provider, tiers in _legacy_routes(table).items()
               for tier, pair in tiers.items()
               for leaf, value in pair.items()}
    written = []
    if write:
        path = str(Path(home) / "settings.json")
        with _locked(home):
            obj = _read(home)
            merged = {"schema_version": 1} if obj is None else copy.deepcopy(obj)
            for key, value in mapping.items():
                *parents, leaf = key.split(".")
                node = merged
                for part in parents:
                    node = node.setdefault(part, {})
                if leaf not in node:
                    node[leaf] = value
                    written.append(key)
            check(merged, path)
            if written:
                _publish(home, merged)
    return {"table": str(table), "mapping": mapping, "written": written}


def _number(text):
    try:
        return int(text)
    except ValueError:  # more digits than int() converts: 0 is outside the range of every integer key
        return 0


def _emit(stream, obj):
    stream.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="readback_settings.py", allow_abbrev=False)
    parser.add_argument("--host", choices=["claude", "codex"])
    parser.add_argument("--cwd")
    parser.add_argument("--registry")
    parser.add_argument("--logbook")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", allow_abbrev=False)
    setter = commands.add_parser("set", allow_abbrev=False)
    setter.add_argument("key")
    setter.add_argument("value")
    migrate = commands.add_parser("migrate-teammates", allow_abbrev=False)
    migrate.add_argument("--table")
    migrate.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if args.host is None:
        parser.error("the host is needed: pass --host claude or --host codex")
    value = None
    if args.command == "set":
        defaults = leaves()
        if args.key not in defaults:
            parser.error("the key must name a leaf of the schema: " + ", ".join(defaults))
        value = args.value
        if type(defaults[args.key]) is int:
            if not re.fullmatch("[0-9]+", value):
                parser.error("the value for " + args.key + " must be a whole number")
            value = _number(value)
    try:
        found = resolve(args.cwd if args.cwd is not None else os.getcwd(), args.host, args.registry, args.logbook)
        if args.command == "migrate-teammates":
            table = args.table or os.environ.get("READBACK_TEAMMATES") or str(Path.home() / "logbook" / "TEAMMATES.md")
            result = migrate_teammates(found["home"], table, args.write)
        else:
            home = found["home"]
            loaded = load(home) if args.command == "show" else update(home, args.key, value)
            result = {"home": home, "status": found["status"], "source": loaded["source"],
                      "digest": loaded["digest"], "settings": loaded["settings"]}
    except SettingsError as exc:
        _emit(sys.stderr.buffer, {"status": exc.code, "error": str(exc)})
        return 3
    _emit(sys.stdout.buffer, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
