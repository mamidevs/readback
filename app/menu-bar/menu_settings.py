"""One-shot settings bridge bundled with the Readback menu bar app."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
from contextlib import nullcontext

import project_registry as registry
import readback_settings as settings


def identifier(path, binding, device, inode):
    # A remembered picker item must expire if its registration or directory is replaced.
    data = [str(path), binding, device, inode]
    return hashlib.sha256(registry.canonical_json(data)).hexdigest()


def item(path, name, binding, identity):
    return {
        "id": identifier(path, binding, identity["device"], identity["inode"]),
        "name": name, "location": str(path),
    }


def catalog(book):
    path = registry._registry_path(book / ".readback" / "projects.json")
    entries = registry.validate_registry(registry.read_json(path))["projects"] if path.exists() else []
    result = {}
    reserved = []
    for entry in entries:
        home = Path(entry["home"])
        reserved.extend([home, *map(Path, entry["retired_roots"])])
        if entry["activation_state"] == "ACTIVE":
            binding = (entry["project_uuid"], entry["home_uuid"])
            project = item(home, entry["slug"], binding, entry["home_identity"])
            result[project["id"]] = (project, binding)

    if book.exists():
        for home in book.iterdir():
            if home.name.startswith(".") or home.is_symlink() or not home.is_dir():
                continue
            if any(registry._overlap(home, path) for path in reserved):
                continue
            # A marked home cannot fall back to a former or missing registration.
            if any(os.path.lexists(home / marker) for marker in ("project.json", registry.REPOSITORY_MARKER)):
                continue
            current = home / "CURRENT.md"
            if current.is_symlink() or not current.is_file():
                continue
            project = item(home, home.name, None, registry.filesystem_identity(home))
            result[project["id"]] = (project, None)
    return result


def access(project, binding, mode):
    home = Path(project["location"])
    with registry._directory_fd(home) as fd:
        info = os.fstat(fd)
        registry.require(identifier(home, binding, info.st_dev, info.st_ino) == project["id"],
                         "The selected project changed", "IDENTITY_MISMATCH")
        # This helper serves exactly one command per process. Pin the settings
        # call to the opened home even if its path is renamed during the call.
        os.fchdir(fd)
        for name in ("settings.json", ".settings.lock"):
            try:
                info = os.lstat(name)
            except FileNotFoundError:
                continue
            registry.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                             "Invalid settings file", "INVALID_SETTINGS")
        loaded = settings.load(".") if mode is None else settings.update(".", "judge.mode", mode)
        return {"mode": loaded["settings"]["judge"]["mode"]}


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("projects")
    commands.add_parser("get").add_argument("id")
    put = commands.add_parser("set")
    put.add_argument("id")
    put.add_argument("mode", choices=("off", "gate", "each"))
    args = parser.parse_args()
    book = Path.home() / "logbook"
    control = book / ".readback"
    try:
        lock = nullcontext()
        if args.command == "set" and control.is_dir():
            lock = registry._lock(control / "projects.lock", time.monotonic() + 5)
        with lock:
            items = catalog(book)
            if args.command == "projects":
                projects = [project for project, binding in items.values()]
                answer = {"projects": sorted(projects, key=lambda p: (p["name"].casefold(), p["location"]))}
            else:
                registry.require(args.id in items, "The selected project is unavailable", "NOT_FOUND")
                project, binding = items[args.id]
                answer = access(project, binding, getattr(args, "mode", None))
        print(json.dumps(answer))
        return 0
    except (settings.SettingsError, registry.RegistryError, OSError) as exc:
        print(json.dumps({"error": getattr(exc, "code", "UNAVAILABLE")}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
