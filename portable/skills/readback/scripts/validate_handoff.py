#!/usr/bin/env python3
"""Structural validator for a Readback contract and handoff.

Usage: python3 scripts/validate_handoff.py CONTRACT HANDOFF

It reads two files and prints one JSON object with the keys `findings`, `valid`
and `validator_version`. It exits 0 when it ran, whatever `valid` is, and 2 on a
usage error or an unreadable file. It reads text only: it runs nothing from
either input, starts no process and writes no file.

Grammar it accepts (PROTOCOL.md 3.2 and 3.3)

Text form
  A key stands at column 0. After it come spaces or tabs and the value. A line
  that starts with a space or a tab continues the value of the key above it.
  Blank lines are ignored. A key is a word of letters, digits, `_` and `-`, or
  the words `Out of scope:` (see the colon keys of a handoff below). Any other
  line at column 0, and a continuation line before the first key, is reported
  as `unresolved`.
  Contract: `CON-<YYYYMMDD>-<NN>  <task>`, then `ACC-<n>  <statement>  Check:
  <check>` lines (a continuation line is kept as a separate physical line), then `Out of scope:`.
  Sections of a contract (version 2). After the line `Out of scope:`, with a
  value or without one, a contract may hold sections. A section starts with a
  line at column 0 that begins with `Limits`, `Definitions`, `Evidence
  requirements`, `Coverage list` or `Command lines`, followed by the end of the
  line, a space or tab, a comma or a colon; a section start is matched before an
  ordinary key. Every physical line after a section start, blank lines and
  indented lines included, belongs to that section until the next section
  start. The section starts with its own line. The sections are kept in
  `extra["sections"]` as a list of `{"name", "lines"}`. Inside a section a line
  at column 0 that begins with `ACC-` or `CON-` and a digit is a V1 defect (`a
  contract line stands inside a section`); any other line of a section that
  begins with `ACC-` or `CON-`, after optional spaces or tabs, is a V1
  `unresolved` finding. No other line of a section is checked. Before the first
  section, and in a contract without sections, the rules of version 1 stand.
  Colon keys of a handoff (version 2). A key of PROTOCOL.md 3.3 at column 0 may
  be followed directly by one colon (`summary: text`). Exactly that colon is
  consumed, and the value keeps the rest of the line with only ASCII spaces and
  tabs removed at both ends, so `summary: task: x` and `summary task: x` both
  hold `task: x`. A colon after any other word is no delimiter. Both forms may
  be mixed in one handoff, and a key that stands twice in either form is
  repeated. A line at column 0 that is no key is not supported: it is kept in
  the value of the key above it and is reported as `unresolved`. In a handoff
  that holds a colon key only the keys of 3.3 are keys, so a line such as
  `profile  x` is such a line too. A kept line never changes how `state` is
  read, and below `checks` it is read as evidence by its own text but is never
  taken for the command of a Check. The
  status bullets `- ACC-<n> <STATUS>` below `contract` are read for V3 and V5,
  and the layout finding stays; a status that is missing because such a line
  could not be read is `unresolved`, and an established defect still gives
  `false`.
  Handoff: one key per line with the keys of PROTOCOL.md 3.3. The value of
  `checks` is one evidence line per physical line. The value of `contract` is
  `ACC-<n> <STATUS>` parts separated by a spaced vertical bar, a semicolon or a
  continuation line. A semicolon inside a parenthesised reason stays in the reason.
  An evidence line is its parts joined with ` | ` (spaces or tabs around the
  bar). A shell command line is `<command> | exit <n> | <result>`; the command
  is the raw text before the one exit-code segment and may hold ` | `. A tool
  call line is `<Tool> tool, ... | no exit code (tool call) | <result>`.

The frame of a stored record (version 2.2)
  A stored record is the publisher's envelope line, the body and the lead's
  lines after an explicit marker. The envelope is one line that starts with
  `<!-- readback ` and ends with `-->` (trailing ASCII spaces, tabs and a
  carriage return are not counted); the lead marker is not an envelope. Only the
  first line that is not blank can be the envelope, in a contract and in a
  handoff. It is left out and gives no finding. A second envelope line, or one
  further down, is an ordinary line.
  The lead marker is a line at column 0 that is exactly `<!-- readback lead
  -->`, with only spaces and tabs after it. Only the first such line counts. In a
  handoff, in any of the three forms, the handoff ends before the marker.
  Limit: text after the lead marker is not part of the handoff and is not
  checked; the marker line is not read either. The lead's lines (`JUDGE`,
  `execution`, `receipt`, `ACCEPTED` and a note) are read only below a marker.
  An indented line, a JSON string and a line that holds the marker text after
  other text never end the handoff. A handoff without the marker is read to its
  end as in version 2.1, so text after a JSON object and a column-0 line that
  starts with `JUDGE` are read as before. A contract is read to its end.

Matching for V4 and V6 (no transformation)
  A check text and a command are the same only when the two raw strings are
  equal with `==` after removing ASCII spaces and tabs at both ends. Nothing
  else is changed: no line joining, no backtick removal, no split on `;`, no
  case folding, no Unicode normalisation, no collapsing. A check that holds a
  newline (it continues on a further physical line, or a JSON string with a
  newline) never matches. An evidence line never matches when it has more than
  one exit-code segment, when it holds a newline (JSON list item), or when the
  command cannot be told with certainty. A V4 defect needs either no
  line with complete or doubtful evidence at all, or a line whose command equals
  the check text and lacks its exit code or has no result. The result is the rest
  of the line after the exit-code segment, as it is; a result that looks like a
  cut marker proves no defect and gives `unresolved`.

JSON form
  One JSON object. Handoff: the keys of PROTOCOL.md 3.3. A value may be a
  string, or a list of strings (joined by newlines), or for `contract` an object
  that maps an ACC id to a status string. Contract: `id`, `task`, `out_of_scope`
  and either one `ACC-<n>` key per statement (a string "statement  Check: x" or
  an object with `statement` and `check`) or an `acc` list of objects with `id`,
  `statement` and `check`.

Extra keys are kept in the `extra` field of the parsed result and are never a
defect, in both forms; a handoff that holds a colon key has no extra key (see
the colon keys above).

Rules V1 to V6 are in the functions `check_contract`, `check_handoff` and
`check_evidence`. A finding has `rule`, `acc`, `level` and `message`. Level
`defect` means a certain break. Level `unresolved` means the validator cannot
tell. `valid` is false for any defect, else "unparsed" for any unresolved
finding, else true. V6 compares text only and is never a defect.
"""

import json
import re
import sys

VALIDATOR_VERSION = "2.2.0"

REQUIRED_KEYS = (
    "id", "from", "date", "state", "readback", "summary", "changes",
    "commit", "checks", "contract", "open", "issues", "next",
)
STATES = ("DONE", "PARTIAL", "BLOCKED", "UNABLE")
STATUSES = ("VERIFIED", "FAILED", "UNVERIFIED")

_KEY_RE = re.compile(r"^(Out of scope:|[A-Za-z][A-Za-z0-9_-]*)(?=[ \t\r]|$)")
_KEY3 = "(" + "|".join(REQUIRED_KEYS) + ")"
_COLON_KEY_RE = re.compile("^" + _KEY3 + ":")
_PLAIN_KEY3_RE = re.compile("^" + _KEY3 + r"(?=[ \t\r]|$)")
_SECTION_RE = re.compile(
    r"^(Limits|Definitions|Evidence requirements|Coverage list|Command lines)(?=[ \t,:]|$)")
_SECTION_DEFECT_RE = re.compile(r"^(?:ACC|CON)-[0-9]")
_SECTION_LINE_RE = re.compile(r"^[ \t]*(?:ACC|CON)-")
_CON_ID_RE = re.compile(r"^CON-\d{8}-\d{2}$")
_ACC_ID_RE = re.compile(r"^ACC-\d+$")
_CHECK_RE = re.compile(r"(?:^|\s)Check:[ \t]*(.*)\Z", re.S)
_STATUS_PART_RE = re.compile(r"^(ACC-\d+)\s+([A-Za-z]+)(?:\s*\((.*)\))?$", re.S)
_EXIT_RE = re.compile(r"^exit(?:[ \t]+code)?[ \t]*[= ]?[ \t]*-?\d+$", re.I)
_SEP_RE = re.compile(r"[ \t]+\|[ \t]+")
_TOOLCALL_RE = re.compile(r"\btool call\b", re.I)
_TOOL_RE = re.compile(r"^[A-Z][A-Za-z]*\s+tool\b")
_EXIT_WORD_RE = re.compile(r"\bexit\b", re.I)
_EXIT_NUM_RE = re.compile(r"\bexit(?:[ \t]+code)?[ \t]*[= ]?[ \t]*-?\d+", re.I)
_CUT_RE = re.compile(
    r"^\(?\s*(?:output\s+)?(?:cut|omitted|truncated|elided|empty|none)\s*\)?\.?$", re.I)


class _Pairs(list):
    """A JSON object read as a list of (key, value) pairs, so repeats survive."""


class _Kept(str):
    """A line at column 0 that is no key, kept below the key above it. It is part of
    the value of that key, but a rule that reads `state` does not take it for the state."""


def _finding(rule, acc, level, message):
    return {"rule": rule, "acc": acc, "level": level, "message": message}


# ---------------------------------------------------------------- parsing

def _split_text(text, colon_keys=False, sections=False):
    """Return ([(key, [lines])], [problem], [section]) for the text form.

    Lines are split at "\n" only. A stored line has only ASCII spaces and tabs
    removed at both ends. With `colon_keys` (a handoff) a PROTOCOL.md 3.3 key may
    be followed directly by one colon, and a line at column 0 that is no key is
    kept below the key above it. With `sections` (a contract) a section start
    after `Out of scope:` opens a section that holds every later physical line.
    """
    pairs, problems, found = [], [], []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    after_scope = False
    colon_form = colon_keys and any(_COLON_KEY_RE.match(raw) for raw in lines)
    for raw in lines:
        start = _SECTION_RE.match(raw) if sections and after_scope else None
        if start:
            found.append({"name": start.group(1), "lines": [raw]})
            continue
        if found:
            found[-1]["lines"].append(raw)
            continue
        if not raw.strip():
            continue
        if raw[0] in " \t":
            if not pairs:
                problems.append("continuation line before any key: %r" % raw.strip()[:60])
            else:
                pairs[-1][1].append(raw.strip(" \t"))
            continue
        if colon_form:
            m = _COLON_KEY_RE.match(raw) or _PLAIN_KEY3_RE.match(raw)
        else:
            m = _KEY_RE.match(raw)
        if not m:
            problems.append("line at column 0 is not a key: %r" % raw[:60])
            if colon_keys and pairs:
                pairs[-1][1].append(_Kept(raw.strip(" \t")))
            continue
        after_scope = after_scope or m.group(1) == "Out of scope:"
        rest = raw[m.end():].strip(" \t")
        pairs.append((m.group(1), [rest] if rest else []))
    return pairs, problems, found


_ENVELOPE_START = "<!-- readback "
_LEAD_MARKER = "<!-- readback lead -->"


def _unframe(text):
    """The text without the publisher's envelope line, when that line is the first line
    that is not blank and is not the lead marker. Any other text is returned as it is."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip():
            if (line.startswith(_ENVELOPE_START) and line.rstrip(" \t\r").endswith("-->")
                    and line.rstrip(" \t") != _LEAD_MARKER):
                return "\n".join(lines[i + 1:])
            break
    return text


def _cut_lead(text):
    """A handoff without its first lead marker line and the lines after it."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.rstrip(" \t") == _LEAD_MARKER:
            return "\n".join(lines[:i])
    return text


def _load_json(text):
    """Return (_Pairs, None) for a JSON object, else (None, reason)."""
    try:
        obj = json.loads(text, object_pairs_hook=_Pairs)
    except ValueError as exc:
        return None, "invalid JSON: %s" % exc
    if not isinstance(obj, _Pairs):
        return None, "JSON input is not an object"
    return obj, None


def _value_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, _Pairs):
        return " | ".join("%s %s" % (k, _value_text(v)) for k, v in value)
    if isinstance(value, list):
        return "\n".join(_value_text(v) for v in value)
    return json.dumps(value, sort_keys=True)


def _looks_json(text):
    return text.lstrip().startswith("{")


def parse_handoff(text):
    """Parse a handoff in text or JSON form.

    Returns a dict: form, ok, pairs (all keys in order), values (first value of
    each key), extra (keys outside PROTOCOL.md 3.3) and problems.
    """
    result = {"form": "text", "ok": True, "pairs": [], "values": {}, "own_values": {}, "extra": {},
              "problems": [], "checks_lines": []}
    text = _cut_lead(_unframe(text))
    if _looks_json(text):
        result["form"] = "json"
        obj, err = _load_json(text)
        if err:
            result["ok"] = False
            result["problems"].append(err)
            return result
        pairs = [(k, _value_text(v)) for k, v in obj]
        for k, v in obj:
            if k == "checks":
                items = v if isinstance(v, list) else _value_text(v).split("\n")
                for item in items:
                    t = _value_text(item)
                    if t.strip(" \t"):
                        result["checks_lines"].append({"text": t.strip(" \t"), "multiline": "\n" in t, "kept": False})
                break
    else:
        raw, problems, _ = _split_text(text, colon_keys=True)
        result["problems"].extend(problems)
        pairs = [(k, "\n".join(lines)) for k, lines in raw]
        for k, lines in raw:
            if k == "checks" and k not in result["own_values"]:
                # a kept line is read by its own text as evidence, but it is never the command of a Check
                result["checks_lines"] = [{"text": l, "multiline": False, "kept": isinstance(l, _Kept)}
                                          for l in lines if l.strip(" \t")]
            result["own_values"].setdefault(k, "\n".join(l for l in lines if not isinstance(l, _Kept)))
        if not pairs:
            result["ok"] = False
            result["problems"].append("no key found in the handoff")
    result["pairs"] = pairs
    for key, value in pairs:
        result["values"].setdefault(key, value)
        if key not in REQUIRED_KEYS:
            result["extra"].setdefault(key, value)
    if result["form"] == "json":
        result["own_values"] = dict(result["values"])
    return result


def _statement(acc_id, statement, check):
    """A statement. `check` is the raw text after `Check:` with only ASCII
    spaces and tabs removed at both ends. `match` is that text, or None when it
    holds a newline and so can never be matched."""
    if check is not None:
        check = check.strip(" \t")
    match = check if check and "\n" not in check else None
    return {"id": acc_id, "statement": statement, "check": check, "match": match}


def _acc_from_text(rest):
    """Split the text after an ACC id into (statement, check or None)."""
    m = _CHECK_RE.search(rest)
    if not m:
        return rest.strip(), None
    return rest[:m.start()].strip(), m.group(1)


def parse_contract(text):
    """Parse a contract in text or JSON form.

    Returns a dict: form, ok, id, ids (every CON id seen), task, statements (a
    list of {id, statement, check}), out_of_scope, extra and problems.
    """
    result = {"form": "text", "ok": True, "id": None, "ids": [], "task": None,
              "statements": [], "out_of_scope": None, "extra": {}, "problems": []}
    text = _unframe(text)
    if _looks_json(text):
        result["form"] = "json"
        obj, err = _load_json(text)
        if err:
            result["ok"] = False
            result["problems"].append(err)
            return result
        entries = [(k, v) for k, v in obj]
        if not entries:
            result["ok"] = False
            result["problems"].append("the contract object is empty")
        for key, value in entries:
            if key == "id":
                result["ids"].append(_value_text(value))
            elif key == "task":
                result["task"] = _value_text(value)
            elif key in ("out_of_scope", "Out of scope"):
                result["out_of_scope"] = _value_text(value)
            elif _ACC_ID_RE.match(key):
                if isinstance(value, _Pairs):
                    d = dict(value)
                    result["statements"].append(_statement(
                        key, _value_text(d.get("statement", d.get("text", ""))),
                        _value_text(d["check"]) if "check" in d else None))
                else:
                    st, ck = _acc_from_text(_value_text(value))
                    result["statements"].append(_statement(key, st, ck))
            elif key in ("acc", "statements") and isinstance(value, list):
                for item in value:
                    if not isinstance(item, _Pairs):
                        result["problems"].append("an item of `%s` is not an object" % key)
                        continue
                    d = dict(item)
                    result["statements"].append(_statement(
                        _value_text(d.get("id", "")),
                        _value_text(d.get("statement", d.get("text", ""))),
                        _value_text(d["check"]) if "check" in d else None))
            else:
                result["extra"].setdefault(key, _value_text(value))
    else:
        raw, problems, found = _split_text(text, sections=True)
        result["problems"].extend(problems)
        if found:
            result["extra"]["sections"] = found
        if not raw:
            result["ok"] = False
            result["problems"].append("no key found in the contract")
        for key, lines in raw:
            if key.startswith("CON-"):
                result["ids"].append(key)
                if result["task"] is None:
                    result["task"] = " ".join(lines)
            elif key.startswith("ACC-"):
                if not _ACC_ID_RE.match(key):
                    result["problems"].append("ACC id cannot be read: %r" % key)
                    continue
                st, ck = _acc_from_text("\n".join(lines))
                result["statements"].append(_statement(key, st, ck))
            elif key == "Out of scope:":
                result["out_of_scope"] = " ".join(lines)
            else:
                result["extra"].setdefault(key, "\n".join(lines))
    if result["ids"]:
        result["id"] = result["ids"][0]
    return result


# ------------------------------------------------------------ rules V1, V2

def check_contract(contract):
    """V1: the contract id and its 1 to 5 ACC statements, each with a Check."""
    out = []
    ids = contract["ids"]
    if not ids:
        out.append(_finding("V1", None, "defect", "the contract has no CON id"))
    else:
        if not _CON_ID_RE.match(ids[0]):
            out.append(_finding("V1", None, "defect",
                                "contract id %r is not CON-<YYYYMMDD>-<NN>" % ids[0]))
        if len(ids) > 1:
            out.append(_finding("V1", None, "defect", "the contract has %d CON ids" % len(ids)))
    statements = contract["statements"]
    if not 1 <= len(statements) <= 5:
        out.append(_finding("V1", None, "defect",
                            "the contract has %d ACC statements, not 1 to 5" % len(statements)))
    seen = set()
    for st in statements:
        acc = st["id"]
        if not _ACC_ID_RE.match(acc):
            out.append(_finding("V1", None, "defect", "ACC id %r is not ACC-<n>" % acc))
            continue
        if acc in seen:
            out.append(_finding("V1", acc, "defect", "%s repeats in the contract" % acc))
        seen.add(acc)
        if not st["check"]:
            out.append(_finding("V1", acc, "defect", "%s has no Check:" % acc))
    sections = contract["extra"].get("sections")
    for section in sections if isinstance(sections, list) else ():
        for line in section["lines"]:
            if _SECTION_DEFECT_RE.match(line):
                out.append(_finding("V1", None, "defect", "a contract line stands inside a section"))
            elif _SECTION_LINE_RE.match(line):
                out.append(_finding("V1", None, "unresolved",
                                    "a line of the section %s begins with ACC- or CON-: %r" % (
                                        section["name"], line.strip(" \t")[:60])))
    return out


def check_handoff(handoff):
    """V2: every key of PROTOCOL.md 3.3, in order, once each, and a valid state."""
    out = []
    keys = [k for k, _ in handoff["pairs"] if k in REQUIRED_KEYS]
    for key in REQUIRED_KEYS:
        n = keys.count(key)
        if n == 0:
            out.append(_finding("V2", None, "defect", "the handoff has no `%s` key" % key))
        elif n > 1:
            out.append(_finding("V2", None, "defect", "the key `%s` appears %d times" % (key, n)))
    first = []
    for key in keys:
        if key not in first:
            first.append(key)
    if first != sorted(first, key=REQUIRED_KEYS.index):
        out.append(_finding("V2", None, "defect", "the handoff keys are not in the order of PROTOCOL.md 3.3"))
    if "state" in handoff["own_values"]:
        state = handoff["own_values"]["state"].strip()
        if state not in STATES:
            out.append(_finding("V2", None, "defect",
                                "state %r is not DONE, PARTIAL, BLOCKED or UNABLE" % state[:40]))
    return out


# ------------------------------------------------------------ rule V3

def status_parts(line):
    """Keep legacy pipe boundaries and split semicolons outside parenthesised reasons."""
    start = depth = 0
    for token in re.finditer(r"\s+\|\s+|[();]", line):
        mark = token.group()
        if mark == "(":
            depth += 1
        elif mark == ")":
            depth = max(0, depth - 1)
        elif mark != ";" or depth == 0:
            yield line[start:token.start()]
            start, depth = token.end(), 0
    yield line[start:]


def parse_statuses(value):
    """Read the `contract` value. Return (entries, unparsed_parts).

    An entry is (acc, status, reason). The status is not checked here.
    """
    entries, unparsed = [], []
    if value.strip().lower() in ("none", "—", "-"):
        return entries, unparsed
    for chunk in value.split("\n"):
        for part in status_parts(chunk):
            part = part.strip()
            if part.startswith("- "):
                part = part[2:].strip()
            if not part:
                continue
            m = _STATUS_PART_RE.match(part)
            if m:
                entries.append((m.group(1), m.group(2), m.group(3)))
            else:
                unparsed.append(part)
    return entries, unparsed


def check_statuses(contract, handoff):
    """V3 and V5. Return (findings, entries)."""
    out = []
    if "contract" not in handoff["values"]:
        return out, []
    entries, unparsed = parse_statuses(handoff["values"]["contract"])
    for part in unparsed:
        out.append(_finding("V3", None, "unresolved",
                            "the contract status %r cannot be read as `ACC-<n> <STATUS>`" % part[:60]))
    contract_ids = []
    for st in contract["statements"]:
        if _ACC_ID_RE.match(st["id"]) and st["id"] not in contract_ids:
            contract_ids.append(st["id"])
    counts = {}
    for acc, status, _ in entries:
        counts[acc] = counts.get(acc, 0) + 1
        if acc not in contract_ids:
            out.append(_finding("V3", acc, "defect", "%s is not an ACC of the contract" % acc))
        if status not in STATUSES:
            out.append(_finding("V3", acc, "defect",
                                "status %r of %s is not VERIFIED, FAILED or UNVERIFIED" % (status, acc)))
    for acc, n in sorted(counts.items()):
        if n > 1:
            out.append(_finding("V3", acc, "defect", "%s has %d statuses" % (acc, n)))
    missing_level = "unresolved" if unparsed else "defect"
    for acc in contract_ids:
        if acc not in counts:
            out.append(_finding("V3", acc, missing_level, "%s has no status" % acc))
    # V5
    if handoff["own_values"].get("state", "").strip() == "DONE":
        for acc in contract_ids:
            sts = [s for a, s, _ in entries if a == acc]
            if not sts:
                out.append(_finding("V5", acc, missing_level, "state DONE but %s has no status" % acc))
            elif any(s != "VERIFIED" for s in sts):
                out.append(_finding("V5", acc, "defect", "state DONE but %s is not VERIFIED" % acc))
    return out, entries


# ------------------------------------------------------------ rules V4, V6

def _line_info(command, matchable, state, doubtful, why=None):
    """`state` is "ok" (complete evidence), "ambiguous" (the result looks like a
    cut marker), "missing" (a certain lack) or None (not judged)."""
    return {"command": command, "matchable": matchable, "complete": state == "ok",
            "ambiguous": state == "ambiguous", "doubtful": doubtful, "why": why}


def _result_state(line, ends, k):
    """Read the result that follows segment k: the rest of the line, as it is."""
    if k >= len(ends):
        return "missing"
    raw = line[ends[k]:].strip(" \t")
    if not raw:
        return "missing"
    if _CUT_RE.match(raw):
        return "ambiguous"
    return "ok"


def parse_evidence_line(line, multiline=False):
    """Read one `checks` line.

    Return {command, matchable, complete, ambiguous, doubtful, why}. `command`
    is raw text with only ASCII spaces and tabs removed at both ends.
    `matchable` is false when the command cannot be told with certainty.
    `complete` is true for full evidence. `ambiguous` means the result looks like
    a cut marker, so it proves no defect. `doubtful` means the line may hold
    evidence that was not read. The result is found by position: it is the rest
    of the line after the exit-code segment (or after the `tool call` segment),
    taken as it is. Words in the result never change how the line is read.
    """
    if multiline:
        return _line_info(None, False, None, True, "the evidence line holds a newline")
    segs, starts, ends, pos = [], [], [], 0
    for m in _SEP_RE.finditer(line):
        segs.append(line[pos:m.start()])
        starts.append(m.start())
        ends.append(m.end())
        pos = m.end()
    segs.append(line[pos:])
    segs = [s.strip(" \t") for s in segs]

    exits = [i for i in range(1, len(segs)) if _EXIT_RE.match(segs[i])]
    if len(exits) > 1:
        return _line_info(None, False, None, True, "the line has more than one exit-code segment")
    head = segs[:exits[0]] if exits else segs
    markers = [i for i in range(1, len(head)) if _TOOLCALL_RE.search(head[i])]
    is_tool = _TOOL_RE.match(segs[0]) is not None or bool(markers)
    if exits and is_tool:
        return _line_info(None, False, None, True, "the line has an exit code and a tool call")
    if exits:
        i = exits[0]
        command = line[:starts[i - 1]].strip(" \t")
        state = _result_state(line, ends, i)
        return _line_info(command, True, state, False,
                          "the command has no result" if state == "missing" else None)
    if is_tool and _EXIT_NUM_RE.search(line):
        return _line_info(None, False, None, True, "the tool call line mentions an exit code that is not a segment")
    if is_tool:
        if markers:
            j = markers[0]
            command = line[:starts[j - 1]].strip(" \t")
            state = _result_state(line, ends, j)
            return _line_info(command, True, state, False,
                              "the tool call has no result" if state == "missing" else None)
        if len(segs) == 1:
            return _line_info(segs[0], True, "missing", False, "the tool call has no result")
        return _line_info(None, False, None, True, "the tool call line cannot be read")
    if _EXIT_WORD_RE.search(line):
        return _line_info(None, False, None, True, "the line mentions an exit code that is not a segment")
    if len(segs) == 1 or (len(segs) == 2 and _CUT_RE.match(segs[1])):
        return _line_info(segs[0], True, "missing", False, "the command has no exit code")
    return _line_info(None, False, None, False, "the command of the line cannot be told")


def _read_checks_line(entry):
    """Read one entry of `checks_lines`. A kept line gives the evidence its own text gives,
    but it can never be matched with the Check text of an ACC."""
    info = parse_evidence_line(entry["text"], entry["multiline"])
    return dict(info, matchable=info["matchable"] and not entry["kept"])


def check_evidence(contract, handoff, entries):
    """V4 and V6."""
    out = []
    if "checks" not in handoff["values"]:
        return out
    lines = [_read_checks_line(l) for l in handoff["checks_lines"]]
    any_evidence = any(l["complete"] or l["ambiguous"] or l["doubtful"] for l in lines)
    checks = {}
    for st in contract["statements"]:
        if _ACC_ID_RE.match(st["id"]) and st["check"] and st["id"] not in checks:
            checks[st["id"]] = st
    for acc in sorted({a for a, s, _ in entries if s in ("VERIFIED", "FAILED") and a in checks}):
        statuses = {s for a, s, _ in entries if a == acc}
        shown = "/".join(s for s in sorted(statuses) if s != "UNVERIFIED")
        match = checks[acc]["match"]
        matched = [l for l in lines if match is not None and l["matchable"] and l["command"] == match]
        if matched:
            if any(l["complete"] for l in matched):
                continue
            if any(l["ambiguous"] for l in matched):
                out.append(_finding("V4", acc, "unresolved",
                                    "%s has a status of %s but the result of its line looks like a cut marker" % (
                                        acc, shown)))
            else:
                out.append(_finding("V4", acc, "defect", "%s has a status of %s but %s: %r" % (
                    acc, shown, matched[0]["why"], match[:60])))
            continue
        if "VERIFIED" in statuses:
            out.append(_finding("V6", acc, "unresolved",
                                "no checks line has the Check: text of %s as its command" % acc))
        if not any_evidence:
            out.append(_finding("V4", acc, "defect", "%s has no evidence line in checks" % acc))
        elif "VERIFIED" not in statuses:
            out.append(_finding("V4", acc, "unresolved",
                                "no checks line can be tied to the Check: text of %s" % acc))
    return out


# ------------------------------------------------------------ entry points

def validate(contract_text, handoff_text):
    """Return {"valid", "findings", "validator_version"} for the two texts."""
    contract = parse_contract(contract_text)
    handoff = parse_handoff(handoff_text)
    findings = []
    for p in contract["problems"]:
        findings.append(_finding("V1", None, "unresolved", p))
    for p in handoff["problems"]:
        findings.append(_finding("V2", None, "unresolved", p))
    if contract["ok"]:
        findings.extend(check_contract(contract))
    if handoff["ok"]:
        findings.extend(check_handoff(handoff))
    if contract["ok"] and handoff["ok"]:
        status_findings, entries = check_statuses(contract, handoff)
        findings.extend(status_findings)
        findings.extend(check_evidence(contract, handoff, entries))
    unique = {json.dumps(f, sort_keys=True): f for f in findings}.values()
    findings = sorted(unique, key=lambda f: (f["rule"], f["acc"] or "", f["level"], f["message"]))
    if any(f["level"] == "defect" for f in findings):
        valid = False
    elif findings:
        valid = "unparsed"
    else:
        valid = True
    return {"valid": valid, "findings": findings, "validator_version": VALIDATOR_VERSION}


def _read(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        sys.stderr.write("usage: validate_handoff.py CONTRACT HANDOFF\n")
        return 2
    try:
        contract_text = _read(argv[0])
        handoff_text = _read(argv[1])
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write("cannot read input: %s\n" % exc)
        return 2
    sys.stdout.write(json.dumps(validate(contract_text, handoff_text), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
