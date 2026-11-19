"""Summarize recent, observable actions of one project session using fixed UI labels."""
from collections import deque
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat

import menu_sessions
import project_registry as registry


TAIL_BYTES = 256 * 1024
HEADER_BYTES = 128 * 1024
EVENT_LIMIT = 12
TOOL_LABELS = {
    "Read": "Dosya okuma isteği", "read_file": "Dosya okuma isteği",
    "Glob": "Dosya arama isteği", "Grep": "İçerik arama isteği",
    "Write": "Dosya yazma isteği", "Edit": "Dosya düzenleme isteği",
    "MultiEdit": "Dosya düzenleme isteği", "apply_patch": "Dosya düzenleme isteği",
    "WebSearch": "Web araması isteği", "WebFetch": "Web sayfasını okuma isteği",
    "Agent": "Alt ajan çağrısı", "Task": "Alt ajan çağrısı",
    "spawn_agent": "Alt ajan çağrısı", "wait_agent": "Alt ajan bekleme isteği",
    "send_message": "Ajana mesaj gönderme isteği", "request_user_input": "Kullanıcıdan bilgi istendi",
}
CODEX_STATES = {
    "task_started": "İşlem turu başladı", "turn_started": "İşlem turu başladı",
    "task_complete": "İşlem turu tamamlandı", "turn_complete": "İşlem turu tamamlandı",
    "turn_aborted": "İşlem turu durduruldu", "exec_command_begin": "Terminal işlemi başladı",
    "exec_command_end": "Terminal işlemi sona erdi", "error": "Hata bildirildi",
}


def command_label(command):
    if not isinstance(command, str):
        return "Terminal komutu isteği"
    try:
        words = shlex.split(command[:2048])
    except ValueError:
        return "Terminal komutu isteği"
    if not words:
        return "Terminal komutu isteği"
    exe = Path(words[0]).name
    args = words[1:]
    if exe in ("pytest", "xctest") or (
        re.fullmatch(r"python[0-9.]*", exe) and args[:2] in (["-m", "pytest"], ["-m", "unittest"])
    ) or (exe in ("npm", "pnpm", "yarn", "cargo", "swift", "go") and args[:1] == ["test"]):
        return "Test çalıştırma isteği"
    if exe in ("make", "xcodebuild", "swiftc") or (
        exe in ("cargo", "swift", "go", "npm", "pnpm", "yarn") and (args[:2] == ["run", "build"] or args[:1] == ["build"])
    ):
        return "Derleme isteği"
    if exe in ("rg", "grep", "find"):
        return "Arama isteği"
    return "Terminal komutu isteği"


def tool_label(name, arguments):
    if not isinstance(name, str):
        return "Araç çağrısı"
    name = name.rsplit(".", 1)[-1]
    if name in ("Bash", "exec_command"):
        if isinstance(arguments, str):
            arguments = object_line(arguments)
        if isinstance(arguments, dict):
            return command_label(arguments.get("command" if name == "Bash" else "cmd"))
        return "Terminal komutu isteği"
    return TOOL_LABELS.get(name, "Araç çağrısı")


def object_line(line):
    try:
        value = json.loads(line)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def display_time(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9T:.+Z-]{10,35}", value):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value
        except ValueError:
            pass
    return None


def model_id(value):
    if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,95}", value):
        return value
    return None


# Codex Desktop also records its tool dispatcher as a custom call. Recognize
# literal calls to known APIs, never evaluate the program or display its input.
DISPATCH_LABELS = {
    "exec_command": "Terminal komutu isteği", "write_stdin": "Terminal işlemini izleme isteği",
    "apply_patch": "Dosya düzenleme isteği", "web__run": "Web araştırması isteği",
    "view_image": "Görsel inceleme isteği",
}


def dispatch_label(source):
    if isinstance(source, str):
        names = re.findall(r"\btools\.(\w+)\s*\(", source[:8192])
        labels = {DISPATCH_LABELS.get(name, "Araç çağrısı") for name in names}
        if len(labels) == 1:
            return labels.pop()
        if len(labels) > 1:
            return "Birden fazla araç çağrıldı"
    return "Araç çağrısı"


def codex_actions(row):
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None, []
    kind = row.get("type")
    item = payload.get("type")
    if not isinstance(item, str):
        item = ""
    if kind == "turn_context":
        return model_id(payload.get("model")), []
    if kind == "event_msg" and item in CODEX_STATES:
        return None, [CODEX_STATES[item]]
    if kind == "response_item":
        if item == "custom_tool_call" and payload.get("name") == "exec":
            return None, [dispatch_label(payload.get("input"))]
        if item in ("function_call", "custom_tool_call"):
            return None, [tool_label(payload.get("name"), payload.get("arguments"))]
        if item in ("function_call_output", "custom_tool_call_output"):
            return None, ["Araç sonucu alındı"]
        if item == "message" and payload.get("role") == "assistant":
            return None, ["Model yanıt yazdı"]
    return None, []


def claude_actions(row):
    message = row.get("message")
    if not isinstance(message, dict):
        return None, []
    kind = row.get("type")
    model = model_id(message.get("model")) if kind == "assistant" else None
    content = message.get("content")
    if not isinstance(content, list):
        return model, []
    labels = []
    for part in content:
        if not isinstance(part, dict):
            continue
        item = part.get("type")
        if kind == "assistant" and item == "tool_use":
            labels.append(tool_label(part.get("name"), part.get("input")))
        elif kind == "assistant" and item == "text":
            labels.append("Model yanıt yazdı")
        elif kind == "user" and item == "tool_result":
            labels.append("Araç hata bildirdi" if part.get("is_error") is True else "Araç sonucu alındı")
    return model, labels


def codex_log(root, session, matches):
    connection = menu_sessions.codex_database(root)
    if connection is None:
        return None
    with closing(connection) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
        model = "model" if "model" in columns else "NULL"
        row = db.execute(f"SELECT cwd,rollout_path,{model} FROM threads WHERE id=? AND archived=0",
                         (session,)).fetchone()
    if not row or not matches(row[0]) or not isinstance(row[1], str):
        return None
    path = Path(row[1])
    if not path.is_relative_to(root / "sessions") or ".." in path.parts:
        return None
    if not path.name.startswith("rollout-") or not path.name.endswith("-" + session + ".jsonl"):
        return None
    return path, row[0], model_id(row[2])


def claude_log(root, session, matches):
    rows, _, _ = menu_sessions.claude_candidates(root, matches)
    for row in rows:
        if row.id == session and matches(row.cwd):
            encoded = re.sub(r"[^a-zA-Z0-9]", "-", row.cwd)
            return root / "projects" / encoded / (session + ".jsonl"), row.cwd, None
    return None


def read_detail(source, host, session, matches):
    path, cwd, model = source

    def project_cwd(value):
        return isinstance(value, str) and (value == cwd or matches(value))

    events = deque(maxlen=EVENT_LIMIT)
    with registry._directory_fd(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return None
            if host == "codex":
                meta = object_line(stream.readline(HEADER_BYTES))
                header = meta.get("payload")
                if (meta.get("type") != "session_meta" or not isinstance(header, dict)
                        or header.get("id") != session or not project_cwd(header.get("cwd"))):
                    return None
            offset = max(0, info.st_size - TAIL_BYTES)
            stream.seek(max(0, offset - 1))
            tail = stream.read(TAIL_BYTES + bool(offset))
    if offset:
        # The extra byte tells us whether the tail starts at a record boundary.
        tail = tail.split(b"\n", 1)[1] if b"\n" in tail else b""
    # A writer can still be appending the final record.
    for line in tail.split(b"\n")[:-1]:
        row = object_line(line)
        if host == "claude":
            if row.get("sessionId") != session or not project_cwd(row.get("cwd")):
                continue
            name, actions = claude_actions(row)
        else:
            name, actions = codex_actions(row)
        model = name or model
        timestamp = display_time(row.get("timestamp"))
        for label in actions:
            events.append({"label": label, "timestamp": timestamp})
    return {"model": model, "events": list(reversed(events)), "available": True}


def for_session(project, binding, book, host, session):
    detail = None
    if menu_sessions.valid_id(session) and host in ("claude", "codex"):
        matches = menu_sessions.project_matcher(project, binding, book)
        try:
            if host == "claude":
                root = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
                source = claude_log(root, session, matches)
            else:
                root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
                source = codex_log(root, session, matches)
            if source:
                detail = read_detail(source, host, session, matches)
        except (OSError, sqlite3.Error, registry.RegistryError):
            pass
    return {"detail": detail or {"model": None, "events": [], "available": False}}
