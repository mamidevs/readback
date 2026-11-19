"""Maintain independently versioned OPEN/NEXT blocks in a UTF-8 CURRENT.md.

Each block is four lines: READBACK-TASK followed by a JSON identity object,
OPEN and NEXT followed by JSON strings, and END-READBACK-TASK. JSON escaping
keeps multiline task content inside its field. Other lines belong to the shared
project summary, including any OPEN/NEXT lines predating this format.
"""
import hashlib
import json
from typing import NamedTuple

from project_registry import RegistryError, require


START = "READBACK-TASK"
END = "END-READBACK-TASK"
FIELDS = ("repository", "task", "worktree")


class Block(NamedTuple):
    start: int
    end: int
    snapshot: dict


def _text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _identity(repository, task, worktree):
    keys = (repository, task, worktree)
    for label, value in zip(FIELDS, keys):
        require(isinstance(value, str) and value.strip() and value.isprintable(),
                f"Task block {label} must be nonempty printable text")
    return keys


def _value(value):
    require(isinstance(value, str)
            and all(c in "\t\n" or c.isprintable() for c in value),
            "Task field must be text with printable characters, tabs or newlines")
    return value


def _fields(open_text, next_text):
    _value(open_text)
    _value(next_text)
    require(4 + open_text.count("\n") + next_text.count("\n") <= 30,
            "Task block exceeds 30 lines")


def _json_field(line, prefix):
    require(line.startswith(prefix), f"Expected {prefix.rstrip()} in task block")
    try:
        return json.loads(line[len(prefix):])
    except (ValueError, RecursionError) as exc:
        raise RegistryError("INVALID", f"Invalid JSON in {prefix.rstrip()}") from exc


def _scan(text):
    # Split only on newline, retaining exact offsets for all surrounding text.
    blocks = {}
    shared = 0
    start = 0
    while start < len(text):
        end = text.find("\n", start)
        end = len(text) if end < 0 else end + 1
        line = text[start:end].rstrip("\r\n")
        if not line.lstrip().startswith((START, END)):
            shared += 1
            start = end
            continue
        identity = _json_field(line, START + " ")
        require(isinstance(identity, dict) and set(identity) == set(FIELDS),
                "Invalid task block identity")
        key = _identity(*(identity[k] for k in FIELDS))
        require(line == START + " " + _text(dict(zip(FIELDS, key))),
                "Task block identity must be canonical")
        require(key not in blocks, "Duplicate task block identity")
        fields = []
        for prefix in ("OPEN      ", "NEXT      ", END):
            pos = end
            require(pos < len(text), "Incomplete task block")
            newline = text.find("\n", pos)
            end = len(text) if newline < 0 else newline + 1
            line = text[pos:end].rstrip("\r\n")
            if prefix == END:
                require(line == END, "Invalid end of task block")
            else:
                fields.append(_json_field(line, prefix))
        opened, following = fields
        _fields(opened, following)
        # A final newline separates blocks. Appending to a document without
        # one must not invalidate a snapshot of the preceding task.
        content = text[start:end].removesuffix("\n")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        blocks[key] = Block(start, end, {"expected_block_sha256": digest,
                                         "open": opened, "next": following})
        start = end
    return blocks, shared


def _decode(current):
    return current.decode("utf-8") if current is not None else ""


def shared_line_count(text):
    """Count only lines of the shared summary, validating all task blocks."""
    return _scan(text)[1]


def snapshot(current, repository, task, worktree):
    key = _identity(repository, task, worktree)
    blocks, _ = _scan(_decode(current))
    block = blocks.get(key)
    if block is None:
        return {"expected_block_sha256": None, "open": "", "next": ""}
    return block.snapshot


def merge(current, repository, task, worktree, expected_block_sha256,
          open_text, next_text, project_name):
    """Replace one block if it matches the snapshot the caller read."""
    key = _identity(repository, task, worktree)
    _fields(open_text, next_text)
    text = _decode(current)
    blocks, _ = _scan(text)
    block = blocks.get(key)
    actual = block.snapshot["expected_block_sha256"] if block else None
    require(actual == expected_block_sha256, "Task block changed; reread before publishing", "CONFLICT")
    header = _text(dict(zip(FIELDS, key)))
    rendered = (f"{START} {header}\nOPEN      {_text(open_text)}\n"
                f"NEXT      {_text(next_text)}\n{END}\n")
    if block:
        return text[:block.start] + rendered + text[block.end:]
    if not text:
        require(isinstance(project_name, str) and project_name.strip() and project_name.isprintable(),
                "Project name must be nonempty printable text")
        text = f"CURRENT   {project_name}\n"
    if not text.endswith("\n"):
        text += "\n"
    return text + rendered


def preserve_blocks(old, new_text):
    """A whole-CURRENT request may edit only the shared part of the document."""
    before, _ = _scan(_decode(old))
    after, _ = _scan(new_text)
    require({k: b.snapshot for k, b in before.items()} == {k: b.snapshot for k, b in after.items()},
            "Whole-CURRENT update must preserve task blocks", "CONFLICT")
