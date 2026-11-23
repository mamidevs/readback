#!/usr/bin/env python3
"""Count the open work of one records folder from its records (CON-20261123-09).

Usage:
  open_work.py --records <folder>

Prints one JSON object on one line, with the keys, in this order:
  records       {"CON": n, "HND": n, "NOTE": n}, the number of records of each kind
  handoffs      {"accepted": [...], "rejected": [...], "replaced": [...], "judged": [...],
                 "open": [...], "conflict": [...]}, the HND ids in their state
  contracts     {"closed": [...], "withdrawn": [...], "open": [...], "unbound": [...]}, the CON ids
  unlinked      the HND ids of the handoffs without `contract_id`
  legacy_notes  [{"id": <NOTE id>, "handoffs": [<HND ids of its first line>], "attributed": <bool>}]
  conflicts     [{"kind": <kind>, "ids": [...]}], each conflict of the rules, none resolved

The ids of a conflict: `decision` and `invalid-note` and `unreadable` the one record id;
`accepted-and-replaced` the handoff, then the handoffs that replace it; `missing-target` the
record that holds the value, then the value; `contract-mismatch` the note, then the handoff;
`withdrawn-and-closed` the withdrawal note, then the contract.

A record is a file `<id>.md` directly in the folder, with the kind CON, HND or NOTE. Each CR run before an LF in a record is read as LF; a CR not before an LF stays. The script
reads no other file, changes no record and writes no file. Every list is sorted by id (string
order); two runs on the same folder print the same bytes. It reads form and links; it does not
show that a verdict is right.

Exit status: 0 for the printed object. 2 for a missing folder or a malformed argument; standard
output stays empty and standard error holds one line with the reason.
"""

import argparse
import json
import os
import re
import sys

sys.dont_write_bytecode = True  # the sibling imports below must leave no .pyc in the scripts folder
import closure_note  # noqa: E402
import validate_handoff  # noqa: E402

RECORD_RE = re.compile(r"\A(CON|HND|NOTE)-[0-9]{8}-[0-9]{2,6}\Z")
HND_IN_TEXT_RE = re.compile(r"(?<![0-9A-Za-z])HND-[0-9]{8}-[0-9]{2,6}(?![0-9A-Za-z])")
LEAD_MARKER = "<!-- readback lead -->"
LINK_STRIP = " \t\r\n"
TARGET_KEYS = {"review": ("handoff", "contract"), "acceptance": ("handoff", "contract", "review"),
               "withdrawal": ("contract", "replaced-by")}
STATES = ("accepted", "rejected", "replaced", "judged", "open", "conflict")
LISTS = ("closed", "withdrawn", "open", "unbound")


class Refuse(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refuse(message)


def parse_args(argv):
    parser = Parser(prog="open_work.py", allow_abbrev=False, add_help=False)
    parser.add_argument("--records", action="append", required=True)
    args = parser.parse_args(argv)
    if len(args.records) != 1:
        raise Refuse("--records stands more than once")
    return args.records[0]


def read_folder(folder):
    """The records of the folder as {id: text or None}; None marks a record that is not UTF-8 text."""
    if not os.path.isdir(folder):
        raise Refuse("--records is not a folder: %r" % folder)
    try:
        names = os.listdir(folder)
    except OSError as err:
        raise Refuse("cannot read the folder %r: %s" % (folder, err.__class__.__name__))
    records = {}
    for name in names:
        if not name.endswith(".md") or not RECORD_RE.match(name[:-3]):
            continue
        path = os.path.join(folder, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as handle:
                records[name[:-3]] = handle.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            records[name[:-3]] = None
    return records


def lf(text):
    """The text with each line end of CR characters and LF read as LF; a CR not before an LF stays.
    Applying it twice gives the same text as applying it once."""
    return re.sub(r"\r+\n", "\n", text)


def kind_of(record_id):
    return record_id.split("-", 1)[0]


def embedded_lines(text):
    """The last JUDGE line and the last ACCEPTED line of a record, each None when absent."""
    lines = lf(text).split("\n")
    for i, line in enumerate(lines):
        if line.rstrip(" \t") == LEAD_MARKER:
            lines = lines[i + 1:]
            break
    found = {}
    for word in ("JUDGE", "ACCEPTED"):
        found[word] = None
        for line in lines:
            if re.match(word + r"[ \t]", line):
                found[word] = line
    return found["JUDGE"], found["ACCEPTED"]


def decision_of(value):
    """The first field of an ACCEPTED value when it is yes or no, else None."""
    first = value.split(" | ", 1)[0].strip(" \t\r")
    return first if first in ("yes", "no") else None


def decision_of_line(line):
    return decision_of(line[len("ACCEPTED"):].strip(" \t\r"))


def first_line(text):
    """The first line that is not blank, after the envelope line."""
    lines = lf(text).split("\n")
    for i, line in enumerate(lines):
        if line.strip():
            if line.startswith("<!-- readback ") and line.rstrip().endswith("-->"):
                del lines[i]
            break
    for line in lines:
        if line.strip():
            return line
    return ""


def link_values(text):
    """The link keys (contract_id, replaces) of a handoff, empty ones left out."""
    parsed = validate_handoff.parse_handoff(lf(text))
    if not parsed["ok"]:
        return {}
    links = {}
    for key in ("contract_id", "replaces"):
        value = parsed["values"].get(key)
        if value is not None and value.strip(LINK_STRIP):
            links[key] = value.strip(LINK_STRIP)
    return links


def analyse(records):
    records = {rid: (None if text is None else lf(text)) for rid, text in records.items()}
    ids = sorted(records)
    conflicts = []
    counts = {"CON": 0, "HND": 0, "NOTE": 0}
    for rid in ids:
        counts[kind_of(rid)] += 1
        if records[rid] is None:
            conflicts.append({"kind": "unreadable", "ids": [rid]})
    readable = [rid for rid in ids if records[rid] is not None]

    handoffs = {}  # id -> {"links", "judge", "decisions"}
    for rid in readable:
        if kind_of(rid) == "HND":
            judge, accepted = embedded_lines(records[rid])
            decisions = []
            if accepted is not None and decision_of_line(accepted):
                decisions.append(decision_of_line(accepted))
            handoffs[rid] = {"links": link_values(records[rid]), "judge": judge is not None, "decisions": decisions}

    notes = []  # valid closure notes: (id, kind, values)
    legacy = []
    for rid in readable:
        if kind_of(rid) != "NOTE":
            continue
        text = records[rid]
        pairs = closure_note.parse_note(text)
        if any(key == "kind" for key, _ in pairs):
            result = closure_note.check_note(text)
            if not result["valid"]:
                conflicts.append({"kind": "invalid-note", "ids": [rid]})
                continue
            values = {}
            for key, value in pairs:
                values.setdefault(key, value)
            notes.append((rid, result["kind"], values))
            continue
        judge, accepted = embedded_lines(text)
        named = sorted(set(HND_IN_TEXT_RE.findall(first_line(text))))
        attributed = first_line(text).startswith("#") and len(named) == 1
        if attributed:
            target = handoffs.get(named[0])
            if target is not None:
                if judge is not None:
                    target["judge"] = True
                if accepted is not None and decision_of_line(accepted):
                    target["decisions"].append(decision_of_line(accepted))
        if judge is not None:
            legacy.append({"id": rid, "handoffs": named, "attributed": attributed})

    reviewed = set()
    for rid, kind, values in notes:
        if kind == "review":
            reviewed.add(values["handoff"])
        elif kind == "acceptance" and values["handoff"] in handoffs:
            handoffs[values["handoff"]]["decisions"].append(decision_of(values["ACCEPTED"]))

    replacers = {}  # handoff id -> sorted ids of the other handoffs whose `replaces` names it
    for rid, info in handoffs.items():
        target = info["links"].get("replaces")
        if target in handoffs and target != rid:
            replacers.setdefault(target, []).append(rid)

    states = {state: [] for state in STATES}
    for rid in sorted(handoffs):
        info = handoffs[rid]
        decisions = info["decisions"]
        replaced = rid in replacers
        if ("yes" in decisions and "no" in decisions) or ("yes" in decisions and replaced):
            state = "conflict"
        elif "yes" in decisions:
            state = "accepted"
        elif "no" in decisions:
            state = "rejected"
        elif replaced:
            state = "replaced"
        elif info["judge"] or rid in reviewed:
            state = "judged"
        else:
            state = "open"
        states[state].append(rid)
        if "yes" in decisions and "no" in decisions:
            conflicts.append({"kind": "decision", "ids": [rid]})
        if "yes" in decisions and replaced:
            conflicts.append({"kind": "accepted-and-replaced", "ids": [rid] + sorted(replacers[rid])})

    contracts = {name: [] for name in LISTS}
    withdrawn_by = {}
    for rid, kind, values in notes:
        if kind == "withdrawal":
            withdrawn_by.setdefault(values["contract"], []).append(rid)
    holders = {}
    for rid, info in handoffs.items():
        if "contract_id" in info["links"]:
            holders.setdefault(info["links"]["contract_id"], []).append(rid)
    for rid in readable:
        if kind_of(rid) != "CON":
            continue
        if any(h in states["accepted"] for h in holders.get(rid, [])):
            name = "closed"
        elif rid in withdrawn_by:
            name = "withdrawn"
        elif rid in holders:
            name = "open"
        else:
            name = "unbound"
        contracts[name].append(rid)
    for contract in sorted(withdrawn_by):
        if contract in contracts["closed"]:
            for note in sorted(withdrawn_by[contract]):
                conflicts.append({"kind": "withdrawn-and-closed", "ids": [note, contract]})

    def missing(source, value):
        if value != "none" and value not in records:
            conflicts.append({"kind": "missing-target", "ids": [source, value]})

    for rid in sorted(handoffs):
        if "replaces" in handoffs[rid]["links"]:
            missing(rid, handoffs[rid]["links"]["replaces"])
    for rid, kind, values in notes:
        for key in TARGET_KEYS[kind]:
            missing(rid, values[key])
        if kind in ("review", "acceptance"):
            info = handoffs.get(values["handoff"])
            if info is not None and "contract_id" in info["links"] and info["links"]["contract_id"] != values["contract"]:
                conflicts.append({"kind": "contract-mismatch", "ids": [rid, values["handoff"]]})

    unlinked = sorted(rid for rid, info in handoffs.items() if "contract_id" not in info["links"])
    unique = []
    for conflict in sorted(conflicts, key=lambda c: (c["kind"], c["ids"])):
        if conflict not in unique:
            unique.append(conflict)
    return {"records": counts, "handoffs": states, "contracts": contracts, "unlinked": unlinked,
            "legacy_notes": sorted(legacy, key=lambda n: n["id"]), "conflicts": unique}


def main(argv):
    try:
        folder = parse_args(argv)
        out = json.dumps(analyse(read_folder(folder))) + "\n"
    except Refuse as err:
        sys.stderr.write(" ".join(str(err).split()) + "\n")
        return 2
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
