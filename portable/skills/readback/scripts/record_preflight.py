#!/usr/bin/env python3
"""Report the form and the links of the records of a publication request (CON-20261123-10).

Usage: record_preflight.py --request <json file> --records <folder>

`--request` is the JSON file that `publish_records.py --request` reads; the
script reads only the list `records`, whose objects hold the texts `id` and
`text`. `--records` is optional; a record of the folder is the file `<id>.md`
directly in it, and its envelope line is left out before use. A record that an
id names is searched in the request first, then in the folder.

It prints one JSON object with `ok` and `records`. `records` holds one entry
for each record of the request, in the order of the request: `id`, `kind` (the
prefix of the id: CON, HND, NOTE, DEC, or `other`), `checked` and `defects`.
An HND entry also holds `validation` and `replaces`. A NOTE entry whose `kind`
line holds `review` or `acceptance` also holds `check` and `links`. Every other
entry has `checked` false and no defect. `ok` is true when no entry holds a
defect. A defect is a text of the form `<name>: <detail>`.

The script reports and refuses nothing: a defect is a line of the report, not a
refusal. It stores nothing, writes no file and changes no record. An exit status
0 does not show that a verdict is right or that an acceptance is justified.

Exit status: 0 when `ok` is true. 1 when it is false. 2 when the request cannot
be read or is no JSON object with a list `records` of objects that hold the
texts `id` and `text`, when a given folder is missing, when an argument is
malformed, or when `validate_handoff.py` or `closure_note.py` cannot be loaded
from the folder of this script (that line starts with `unavailable:`); then
standard output stays empty and standard error holds one line with the reason.
"""

import importlib.util
import json
import os
import re
import sys

sys.dont_write_bytecode = True  # a helper that is loaded below must leave no .pyc file next to it

HERE = os.path.dirname(os.path.abspath(__file__))
ENVELOPE_START = "<!-- readback "
LEAD_MARKER = "<!-- readback lead -->"
KIND_RE = re.compile(r"\A(CON|HND|NOTE|DEC)-")
SAFE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]*\Z")
KEY_RE = re.compile(r"\A(\S+)[ \t]+(.*)\Z")
LINK_KEYS = ("handoff", "contract", "review")
CHECKED_KINDS = ("review", "acceptance")  # the note kinds that this script checks; any other kind line is left alone
STRIP = " \t\r\n"


class Refuse(Exception):
    pass


def one_line(text):
    return " ".join(str(text).split())


def load_helper(name):
    path = os.path.join(HERE, name + ".py")
    try:
        spec = importlib.util.spec_from_file_location("preflight_helper_" + name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (Exception, SystemExit) as err:  # a missing file, a syntax error or an error at import
        raise Refuse("unavailable: %s.py cannot be loaded: %s" % (name, err.__class__.__name__))


def parse_args(argv):
    options = {}
    i = 0
    while i < len(argv):
        name = argv[i]
        if name not in ("--request", "--records"):
            raise Refuse("malformed argument: %s" % one_line(name))
        if name in options:
            raise Refuse("%s is given twice" % name)
        if i + 1 >= len(argv):
            raise Refuse("%s needs a value" % name)
        options[name] = argv[i + 1]
        i += 2
    if "--request" not in options:
        raise Refuse("usage: record_preflight.py --request <json file> [--records <folder>]")
    return options["--request"], options.get("--records")


def read_utf8(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


def read_request(path):
    try:
        request = json.loads(read_utf8(path))
    except (OSError, ValueError, RecursionError) as err:
        raise Refuse("cannot read the request %s: %s" % (one_line(path), err.__class__.__name__))
    if not isinstance(request, dict) or not isinstance(request.get("records"), list):
        raise Refuse("the request is no JSON object with a list records")
    for item in request["records"]:
        if not (isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("text"), str)):
            raise Refuse("a record of the request is no object with the texts id and text")
    return [(item["id"], item["text"]) for item in request["records"]]


def unframe(text):
    """The text without the publisher's envelope line: the first line that is not blank, when it
    starts with `<!-- readback ` and ends with `-->` and is not the lead marker."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip():
            if (line.startswith(ENVELOPE_START) and line.rstrip(" \t\r").endswith("-->")
                    and line.rstrip(" \t") != LEAD_MARKER):
                return "\n".join(lines[i + 1:])
            break
    return text


class Lookup:
    def __init__(self, request, folder):
        self.request = request
        self.folder = folder

    def find(self, record_id):
        """The text of the record that an id names, or None. The request is searched first."""
        for rid, text in self.request:
            if rid == record_id:
                return text
        if self.folder is None or not SAFE_ID_RE.match(record_id):
            return None
        path = os.path.join(self.folder, record_id + ".md")
        if not os.path.isfile(path):
            return None
        try:
            return unframe(read_utf8(path))
        except (OSError, UnicodeDecodeError):
            return None


def kind_of(record_id):
    match = KIND_RE.match(record_id)
    return match.group(1) if match else "other"


def handoff_entry(entry, text, lookup, helpers):
    """The handoff rules: `validation` and `replaces`, with their defects."""
    validate_handoff = helpers["validate"]
    defects = []
    parsed = validate_handoff.parse_handoff(text)
    if not parsed["ok"]:
        entry["validation"] = "unreadable"
        entry["replaces"] = "absent"
        defects.append("validation: unreadable")
    else:
        values = parsed["values"]
        contract_id = values["contract_id"].strip(STRIP) if "contract_id" in values else None
        replaces = values["replaces"].strip(STRIP) if "replaces" in values else None
        if contract_id is None:
            entry["validation"] = "unbound"
        else:
            contract_text = lookup.find(contract_id)
            if contract_text is None:
                entry["validation"] = "contract-missing"
                defects.append("contract-missing: " + contract_id)
            else:
                result = validate_handoff.validate(contract_text, text)
                if result["valid"] is True:
                    entry["validation"] = "true"
                elif result["valid"] is False:
                    count = sum(1 for f in result["findings"] if f["level"] == "defect")
                    entry["validation"] = "false %d" % count
                    defects.append("validation: " + entry["validation"])
                else:
                    entry["validation"] = "unparsed"
        if replaces is None:
            entry["replaces"] = "absent"
        elif replaces == "none":
            entry["replaces"] = "none"
        elif lookup.find(replaces) is not None:
            entry["replaces"] = "found"
        else:
            entry["replaces"] = "missing"
            defects.append("replaces-missing: " + replaces)
    entry["defects"] = defects


def note_keys(text):
    """The first value of each key line of a note, without the envelope line."""
    values = {}
    for line in unframe(text).split("\n"):
        match = KEY_RE.match(line.rstrip("\r"))
        if match:
            values.setdefault(match.group(1), match.group(2).strip())
    return values


def note_entry(entry, text, values, lookup, helpers):
    """The note rules: `check` and `links`, with their defects."""
    check_note = helpers["closure"].check_note
    parse_handoff = helpers["validate"].parse_handoff
    checked = check_note(text)
    defects = ["check: " + finding for finding in checked["findings"]]
    entry["check"] = checked["valid"]
    links = []
    found = {}
    for key in LINK_KEYS:
        if key in values and values[key] != "none":
            found[key] = lookup.find(values[key])
            links.append({"key": key, "value": values[key], "state": "missing" if found[key] is None else "found"})
    entry["links"] = links
    for link in links:
        if link["state"] == "missing":
            defects.append("link-missing: %s %s" % (link["key"], link["value"]))
    if found.get("handoff") is not None and "contract" in values:
        parsed = parse_handoff(found["handoff"])
        if parsed["ok"] and "contract_id" in parsed["values"]:
            contract_id = parsed["values"]["contract_id"].strip(STRIP)
            if values["contract"] != contract_id:
                defects.append("contract-mismatch: %s %s" % (values["contract"], contract_id))
    if values["kind"] == "acceptance" and found.get("review") is not None:
        review_text = found["review"]
        review_valid = (note_keys(review_text).get("kind") == "review" and check_note(review_text)["valid"])
        if not review_valid or note_keys(review_text).get("handoff") != values.get("handoff"):
            defects.append("review-mismatch: " + values["review"])
    entry["defects"] = defects


def entry_for(record_id, text, lookup, helpers):
    entry = {"id": record_id, "kind": kind_of(record_id), "checked": False}
    if entry["kind"] == "HND":
        entry["checked"] = True
        handoff_entry(entry, text, lookup, helpers)
    elif entry["kind"] == "NOTE":
        values = note_keys(text)
        if values.get("kind") in CHECKED_KINDS:
            entry["checked"] = True
            note_entry(entry, text, values, lookup, helpers)
    entry.setdefault("defects", [])
    return entry


def build(request, folder, helpers):
    lookup = Lookup(request, folder)
    records = [entry_for(rid, text, lookup, helpers) for rid, text in request]
    return {"ok": not any(e["defects"] for e in records), "records": records}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        request_path, folder = parse_args(argv)
        helpers = {"validate": load_helper("validate_handoff"), "closure": load_helper("closure_note")}
        request = read_request(request_path)
        if folder is not None and not os.path.isdir(folder):
            raise Refuse("the folder %s is missing" % one_line(folder))
        result = build(request, folder, helpers)
        out = json.dumps(result) + "\n"
    except Refuse as err:
        sys.stderr.write(one_line(err) + "\n")
        return 2
    except Exception as err:
        sys.stderr.write("cannot check the records: %s\n" % err.__class__.__name__)
        return 2
    sys.stdout.write(out)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
