#!/usr/bin/env python3
"""Print a handoff skeleton from a contract (PROTOCOL.md 3.2 and 3.3).

Usage: python3 scripts/handoff_skeleton.py [--text] CONTRACT

It reads one contract through `validate_handoff.parse_contract` and prints one
JSON object with the keys of PROTOCOL.md 3.3 in their order. With `--text` it
prints the same content in the spaced key form. In `checks` stands one string
`<check text> | exit <n> | <result>` for each distinct Check text, in contract
order. In `contract` stands every ACC id with the placeholder
`<VERIFIED, FAILED or UNVERIFIED>`. The other values are placeholders in angle
brackets; `readback` names the ACC ids.

The JSON form survives a report that arrives with leading spaces; the text form
does not. The skeleton fixes the form of a handoff, not its truth: an evidence
line that the worker adds by hand can still hold a wrong escape.

Exit status: 0 when it printed a skeleton. 2 when the contract cannot be read,
holds no statement, or holds a Check text that the validator can never match
(no Check, or a line break in it, see "Matching for V4 and V6" in
validate_handoff.py); then standard output stays empty and standard error holds
one line with the reason. It starts no process and writes no file.
"""

import json
import sys

sys.dont_write_bytecode = True  # the sibling import below must leave no .pyc file next to the script
import validate_handoff  # noqa: E402

STATUS = "<VERIFIED, FAILED or UNVERIFIED>"
INDENT = " " * 10


class Refused(Exception):
    pass


def distinct_checks(contract):
    """The distinct Check texts in contract order, or Refused."""
    if not contract["statements"]:
        raise Refused("the contract holds no statement")
    checks = []
    for st in contract["statements"]:
        if not st["check"]:
            raise Refused("%s has no Check text" % (st["id"] or "a statement"))
        if st["match"] is None:
            raise Refused("the Check text of %s holds a line break and can never match" % st["id"])
        if st["match"] not in checks:
            checks.append(st["match"])
    return checks


def build(contract):
    checks = distinct_checks(contract)
    ids = []
    for st in contract["statements"]:
        if st["id"] not in ids:
            ids.append(st["id"])
    return [
        ("id", "HND-<YYYYMMDD>-<NN>"),
        ("from", "<agent role>"),
        ("date", "<YYYY-MM-DD>"),
        ("state", "<DONE, PARTIAL, BLOCKED or UNABLE>"),
        ("readback", "task: <what I will do> | limits: <what I must not change> | done when: " + ", ".join(ids)),
        ("summary", "<1 to 3 sentences>"),
        ("changes", ["<file: what changed>"]),
        ("commit", "<repo@sha or none>"),
        ("checks", [c + " | exit <n> | <result>" for c in checks]),
        ("contract", [(i, STATUS) for i in ids]),
        ("open", "<the work that remains and why, or —>"),
        ("issues", ["<severity: description, or —>"]),
        ("next", "<the first step for the receiver>"),
    ]


def as_json(pairs):
    obj = {}
    for key, value in pairs:
        obj[key] = dict(value) if key == "contract" else value
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def as_text(pairs):
    out = []
    for key, value in pairs:
        if key == "contract":
            lines = ["%s %s" % item for item in value]
        else:
            lines = value if isinstance(value, list) else [value]
        out.append(key.ljust(10) + lines[0])
        out.extend(INDENT + line for line in lines[1:])
    return "\n".join(out) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    text_form = "--text" in argv
    args = [a for a in argv if a != "--text"]
    if len(args) != 1:
        sys.stderr.write("usage: handoff_skeleton.py [--text] CONTRACT\n")
        return 2
    try:
        with open(args[0], "rb") as fh:
            text = fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write("cannot read the contract: %s\n" % " ".join(str(exc).split()))
        return 2
    try:
        pairs = build(validate_handoff.parse_contract(text))
    except Refused as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    sys.stdout.write(as_text(pairs) if text_form else as_json(pairs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
