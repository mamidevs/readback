#!/usr/bin/env python3
"""Rebuildable discovery hints for explicitly selected project documents.

The catalog is never a source of authority. Every query rechecks the document
inventory; this bounds agent context, rather than promising fewer disk reads.
Only generated files are written. No provider, database, or network is used.
Filename exclusions are not a content-level secret detector.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time
import uuid

VERSION = 1
DEFAULT_MAX_BYTES = 1024 * 1024
CATALOG = "project-index.json"
OVERVIEW = "PROJECT-INDEX.md"
EXTENSIONS = {".md", ".txt"}
EXCLUDED_DIRECTORIES = {"outputs", "node_modules", "vendor", "legacy", "recovery", "backups", "archive", "archives", "__pycache__"}
SENSITIVE_NAME = re.compile(r"(?:^|[._ -])(credentials?|secrets?|passwords?|tokens?|api[._ -]?keys?|private[._ -]?keys?)(?:[._ -]|$)", re.I)
REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class IndexError(Exception):
    def __init__(self, message, status="INVALID"):
        super().__init__(message)
        self.status = status


def require(condition, message, status="INVALID"):
    if not condition:
        raise IndexError(message, status)


def canonical_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def safe_parts(value):
    require(isinstance(value, str) and value and "\\" not in value, "Expected a relative document directory")
    parts = PurePosixPath(value).parts
    require(not PurePosixPath(value).is_absolute() and all(part not in (".", "..") for part in parts), "Document directories must stay under the home")
    require(all(not part.startswith(".") and part.lower() not in EXCLUDED_DIRECTORIES and not SENSITIVE_NAME.search(part) for part in parts), "Excluded document directory")
    require("/".join(parts) == value, "Document directories must use canonical relative paths")
    return parts


@contextlib.contextmanager
def open_home(home):
    """Open each absolute path component without following links."""
    path = Path(os.path.abspath(os.path.expanduser(str(home))))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            new_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new_fd
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def open_relative_directory(home_fd, parts):
    fd = os.dup(home_fd)
    try:
        for part in parts:
            new_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new_fd
        yield fd
    finally:
        os.close(fd)


def read_regular(parent_fd, name, max_bytes):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "Expected an independent regular file")
        require(before.st_size <= max_bytes, "File exceeds the size limit")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(max_bytes + 1)
        after = os.fstat(fd)
        require(len(data) <= max_bytes and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "A document changed while reading", "SOURCE_CHANGED")
        return data
    finally:
        os.close(fd)


def validate_config(roots, repos, max_bytes):
    require(isinstance(roots, list) and roots and all(isinstance(root, str) for root in roots), "Provide document directories")
    normalized = sorted(set(roots))
    for root in normalized:
        safe_parts(root)
    require(len(normalized) == len(roots), "Duplicate document directories")
    require(not any(right.startswith(left + "/") for left in normalized for right in normalized if left != right), "Document directories cannot overlap")
    require(isinstance(repos, list) and all(isinstance(repo, str) and REPO_NAME.fullmatch(repo) for repo in repos), "Invalid repository name")
    require(len(repos) == len(set(repos)), "Duplicate repository names")
    require(type(max_bytes) is int and 1 <= max_bytes <= 16 * 1024 * 1024, "Document limit must be 1 byte to 16 MiB")
    return {"roots": normalized, "repos": sorted(repos), "max_bytes": max_bytes}


def document_metadata(path, data, repos):
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return None, "non_utf8"
    if "\0" in text:
        return None, "binary"
    headings = []
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match and len(headings) < 50:
            headings.append({"line": number, "text": match.group(1)[:240]})
    title = headings[0]["text"] if headings else PurePosixPath(path).stem[:240]
    mentioned = [repo for repo in repos if re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(repo) + r"(?![A-Za-z0-9_.-])", text)]
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "title": title, "line": headings[0]["line"] if headings else 1,
            "headings": headings, "repo_mentions": mentioned,
            "scope_hint": "explicit_mentions" if mentioned else "shared_or_unclassified"}, None


def scan(home_fd, config):
    entries, skipped = [], []

    def skip(path, reason):
        skipped.append({"path": path, "reason": reason})

    def walk(directory_fd, relative):
        for name in sorted(os.listdir(directory_fd)):
            path = relative + "/" + name
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                skip(path, "symlink")
            elif name.startswith("."):
                skip(path, "hidden")
            elif stat.S_ISDIR(info.st_mode):
                if name.lower() in EXCLUDED_DIRECTORIES or SENSITIVE_NAME.search(name):
                    skip(path, "excluded_directory")
                    continue
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    actual = os.fstat(child)
                    require((info.st_dev, info.st_ino) == (actual.st_dev, actual.st_ino), "A directory changed while scanning", "SOURCE_CHANGED")
                    walk(child, path)
                finally:
                    os.close(child)
            elif not stat.S_ISREG(info.st_mode):
                skip(path, "not_regular")
            elif info.st_nlink != 1:
                skip(path, "hardlink")
            elif name == OVERVIEW:
                skip(path, "generated_overview")
            elif SENSITIVE_NAME.search(name):
                skip(path, "sensitive_filename")
            elif PurePosixPath(name).suffix.lower() not in EXTENSIONS:
                skip(path, "unsupported_extension")
            elif info.st_size > config["max_bytes"]:
                skip(path, "size_limit")
            else:
                data = read_regular(directory_fd, name, config["max_bytes"])
                metadata, reason = document_metadata(path, data, config["repos"])
                if reason:
                    skip(path, reason)
                else:
                    entries.append(metadata)
    for root in config["roots"]:
        with open_relative_directory(home_fd, safe_parts(root)) as directory_fd:
            walk(directory_fd, root)
    return {"entries": sorted(entries, key=lambda entry: entry["path"]),
            "skipped": sorted(skipped, key=lambda item: item["path"])}


def overview(catalog):
    def escape(text):
        return text.replace("\n", " ").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")
    from urllib.parse import quote
    lines = ["# Project document index", "", "Generated discovery hints; original documents remain authoritative.",
             "Repository mentions indicate text matches, not ownership or current validity.",
             "Query computes current hints without writing; stale caches are reported for lead rebuild.", "",
             f"Indexed documents: {len(catalog['entries'])}. Skipped entries: {len(catalog['skipped'])}.", "",
             "## Selected document directories", "", "| Directory | Documents |", "|---|---:|"]
    for root in catalog["config"]["roots"]:
        count = sum(entry["path"].startswith(root + "/") for entry in catalog["entries"])
        lines.append(f"| {escape(root)} | {count} |")
    lines.extend(["", "## Repository mention counts", "", "| Repository | Documents |", "|---|---:|"])
    for repo in catalog["config"]["repos"]:
        count = sum(repo in entry["repo_mentions"] for entry in catalog["entries"])
        lines.append(f"| {escape(repo)} | {count} |")
    count = sum(not entry["repo_mentions"] for entry in catalog["entries"])
    lines.extend([f"| No repository mentions (still visible in every repo query) | {count} |", "", "## Document sample", ""])
    for entry in catalog["entries"][:30]:
        lines.append(f"- [{escape(entry['title'])}]({quote(entry['path'], safe='/')}) — line {entry['line']}")
    if len(catalog["entries"]) > 30:
        lines.append(f"- {len(catalog['entries']) - 30} more documents in `.readback/project-index.json`.")
    lines.extend(["", "Use `project_index.py query --home <home> --repo <name> --text <term>` for scoped discovery.",
                  "Use `project_index.py check --home <home>` to check freshness and inspect skipped entries.", ""])
    return "\n".join(lines).encode("utf-8")


def check_output(parent_fd, name):
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "Generated output must be an independent regular file")


def atomic_write(parent_fd, name, data):
    check_output(parent_fd, name)
    temporary = ".index-" + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        check_output(parent_fd, name)
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def index_directory(home_fd, create=False):
    if create:
        try:
            os.mkdir(".readback", mode=0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
    fd = os.open(".readback", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_fd)
    try:
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def publication_lock(directory_fd):
    fd = os.open("project-index.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "Invalid index lock")
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, "Another index builder is active", "BUSY")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def build(home, roots, repos, max_bytes=DEFAULT_MAX_BYTES):
    config = validate_config(roots, repos, max_bytes)
    with open_home(home) as home_fd:
        # Validate the roots before creating generated output directories.
        initial = scan(home_fd, config)
        with index_directory(home_fd, create=True) as index_fd, publication_lock(index_fd):
            current = scan(home_fd, config)
            require(initial == current, "Documents changed during index construction", "SOURCE_CHANGED")
            catalog = {"index_version": VERSION, "authority": "discovery_only", "config": config, **current}
            check_output(home_fd, OVERVIEW)
            check_output(index_fd, CATALOG)
            atomic_write(index_fd, CATALOG, canonical_bytes(catalog))
            atomic_write(home_fd, OVERVIEW, overview(catalog))
            return {"status": "BUILT", "documents": len(current["entries"]), "skipped_count": len(current["skipped"]),
                    "skipped": current["skipped"], "catalog": ".readback/" + CATALOG, "overview": OVERVIEW}


def load_catalog(home_fd):
    with index_directory(home_fd) as index_fd:
        data = read_regular(index_fd, CATALOG, 64 * 1024 * 1024)
    try:
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                require(key not in result, "Duplicate catalog key")
                result[key] = value
            return result
        catalog = json.loads(data, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError) as error:
        raise IndexError("Invalid catalog JSON") from error
    require(type(catalog) is dict and set(catalog) == {"index_version", "authority", "config", "entries", "skipped"}, "Invalid catalog fields")
    require(catalog["index_version"] == VERSION and catalog["authority"] == "discovery_only", "Unsupported index")
    config = catalog["config"]
    require(type(config) is dict and set(config) == {"roots", "repos", "max_bytes"}, "Invalid catalog configuration")
    require(config == validate_config(**config), "Catalog configuration is not canonical")
    require(type(catalog["entries"]) is list and type(catalog["skipped"]) is list, "Invalid catalog inventory")
    return catalog


def compare(catalog, current):
    # Compare complete generated metadata, not just hashes: editing catalog hints
    # also invalidates the cache instead of making them authoritative.
    if catalog["entries"] == current["entries"] and catalog["skipped"] == current["skipped"]:
        return None
    old = {entry.get("path"): entry for entry in catalog["entries"] if isinstance(entry, dict) and isinstance(entry.get("path"), str)}
    new = {entry["path"]: entry for entry in current["entries"]}
    return {"status": "STALE", "action": "Lead should rebuild the saved index; queries compute fresh hints.",
            "added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
            "changed": sorted(path for path in new.keys() & old.keys() if old[path] != new[path]),
            "skips_changed": catalog["skipped"] != current["skipped"]}


def check(home):
    with open_home(home) as home_fd:
        catalog = load_catalog(home_fd)
        current = scan(home_fd, catalog["config"])
        return compare(catalog, current) or {"status": "FRESH", "documents": len(current["entries"]),
                                             "skipped_count": len(current["skipped"]), "skipped": current["skipped"]}


def query(home, repo=None, text=None, limit=20, offset=0):
    require(type(limit) is int and 1 <= limit <= 1000, "Query limit must be 1 to 1000")
    require(type(offset) is int and offset >= 0, "Query offset must be nonnegative")
    require(text is None or isinstance(text, str), "Invalid query text")
    with open_home(home) as home_fd:
        catalog = load_catalog(home_fd)
        require(repo is None or repo in catalog["config"]["repos"], "Repository is not in the index configuration")
        current = scan(home_fd, catalog["config"])
        stale = compare(catalog, current)
        matches = []
        needle = text.casefold() if text else None
        for entry in current["entries"]:
            result = dict(entry)
            if needle:
                parts = PurePosixPath(entry["path"]).parts
                with open_relative_directory(home_fd, parts[:-1]) as parent_fd:
                    data = read_regular(parent_fd, parts[-1], catalog["config"]["max_bytes"])
                require(hashlib.sha256(data).hexdigest() == entry["sha256"],
                        "A document changed during the query; retry the query.", "SOURCE_CHANGED")
                lines = data.decode("utf-8").splitlines()
                match_line = next((number for number, line in enumerate(lines, 1) if needle in line.casefold()), None)
                if match_line is None and needle not in entry["path"].casefold():
                    continue
                result["match_line"] = match_line
            matches.append(result)

        def group(entry):
            if repo and repo in entry["repo_mentions"]:
                return "requested_repo_mentions"
            return "other_repo_mentions" if entry["repo_mentions"] else "no_repo_mentions"

        priority = {"requested_repo_mentions": 0, "no_repo_mentions": 1, "other_repo_mentions": 2}
        matches.sort(key=lambda entry: (priority[group(entry)], entry["path"]))
        selected = matches[offset:offset + limit]
        counts = {name: {"total": sum(group(entry) == name for entry in matches),
                         "returned": sum(group(entry) == name for entry in selected)}
                  for name in priority}
        result = {"status": "FRESH", "authority": "discovery_only", "repo": repo,
                  "scope_note": "All documents remain visible. Repository mentions rank discovery hints; they never define ownership or filter scope.",
                  "stale_cache": stale is not None, "cache_action": "Lead should rebuild the saved index." if stale else None,
                  "total": len(matches), "returned": len(selected), "offset": offset,
                  "truncated": len(selected) < len(matches),
                  "next_offset": offset + len(selected) if offset + len(selected) < len(matches) else None,
                  "class_counts": counts, "skipped_count": len(current["skipped"]), "results": selected}
        if stale:
            result["cache_changes"] = stale
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--home", required=True)
    build_parser.add_argument("--roots", nargs="+", required=True)
    build_parser.add_argument("--repos", nargs="*", default=[])
    build_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    query_parser = commands.add_parser("query")
    query_parser.add_argument("--home", required=True)
    query_parser.add_argument("--repo")
    query_parser.add_argument("--text")
    query_parser.add_argument("--limit", type=int, default=20)
    query_parser.add_argument("--offset", type=int, default=0)
    check_parser = commands.add_parser("check")
    check_parser.add_argument("--home", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build(args.home, args.roots, args.repos, args.max_bytes)
        elif args.command == "query":
            result = query(args.home, args.repo, args.text, args.limit, args.offset)
        else:
            result = check(args.home)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2 if result["status"] == "STALE" else 0
    except (IndexError, OSError) as error:
        print(json.dumps({"status": getattr(error, "status", "IO_ERROR"), "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
