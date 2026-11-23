#!/usr/bin/env python3
"""Print and check the closure notes of a handoff (CON-20261123-08).

Usage:
  closure_note.py review --handoff <HND id> --contract <CON id> --helper-output <file>
  closure_note.py acceptance --handoff <HND id> --contract <CON id>
      --review <NOTE id or none> --accepted <yes or no> --date <YYYY-MM-DD>
      [--reason <text>] [--rests-on <text>]...
  closure_note.py check <file>

A review note holds one judge call: the lines `kind review`, `handoff`,
`contract`, then the first three lines of the helper output unchanged (JUDGE,
execution, receipt). An acceptance note holds `kind acceptance`, `handoff`,
`contract`, `review`, `ACCEPTED <yes or no> | <date>` with ` | <reason>` when a
reason is given, and one `rests-on` line for each `--rests-on`.

`check` reads the form of one note and prints one JSON object with `valid`,
`kind` and `findings`. It leaves out the publisher's envelope line when that is
the first line that is not blank. It does not show that the review exists, that
the judge said PASS, or that the acceptance is justified.

Exit status: 0 for a printed note, or a valid note. 1 for a note with a defect.
2 for a missing or malformed argument, `--accepted no` without a reason, or a
file that cannot be read; standard output stays empty and standard error holds
one line with the reason. The script writes no file.
"""

import argparse
import datetime
import json
import re
import sys

ID_RE = {kind: re.compile(r"\A%s-\d{8}-\d{2,6}\Z" % kind) for kind in ("CON", "HND", "NOTE")}
DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
KEY_RE = re.compile(r"\A(\S+)[ \t]+(.*)\Z")
KEYS = {"review": ["kind", "handoff", "contract", "JUDGE", "execution", "receipt"],
        "acceptance": ["kind", "handoff", "contract", "review", "ACCEPTED"]}
JUDGE_WORDS = ("PASS", "FAIL", "none", "off", "skipped")
HELPER_KEYS = ("JUDGE", "execution", "receipt")


class Refuse(Exception):
    pass


def valid_date(text):
    if not DATE_RE.match(text):
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def read_text(path, what):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError) as err:
        raise Refuse("cannot read %s %s: %s" % (what, path, err.__class__.__name__))


def need_id(kind, value, option):
    if not ID_RE[kind].match(value or ""):
        raise Refuse("%s is not a %s id: %r" % (option, kind, value))
    return value


def no_break(value, option):
    if re.search(r"[\r\n]", value):
        raise Refuse("%s holds a line break" % option)
    return value


def make_review(args):
    handoff = need_id("HND", args.handoff, "--handoff")
    contract = need_id("CON", args.contract, "--contract")
    lines = read_text(args.helper_output, "the helper output").split("\n")[:3]
    lines = [line.rstrip("\r") for line in lines]
    for key, line in zip(HELPER_KEYS, lines + [""] * (3 - len(lines))):
        if not re.match(r"%s[ \t]+\S" % key, line):
            raise Refuse("the helper output has no %s line in its first three lines" % key)
    return ["kind review", "handoff " + handoff, "contract " + contract] + lines


def make_acceptance(args):
    handoff = need_id("HND", args.handoff, "--handoff")
    contract = need_id("CON", args.contract, "--contract")
    if args.review != "none":
        need_id("NOTE", args.review, "--review")
    if args.accepted not in ("yes", "no"):
        raise Refuse("--accepted is neither yes nor no: %r" % args.accepted)
    if not valid_date(args.date):
        raise Refuse("--date is not a date in the form YYYY-MM-DD: %r" % args.date)
    reason = no_break(args.reason, "--reason").strip() if args.reason is not None else ""
    if args.accepted == "no" and not reason:
        raise Refuse("--accepted no needs a --reason")
    accepted = "ACCEPTED %s | %s" % (args.accepted, args.date)
    if reason:
        accepted += " | " + reason
    lines = ["kind acceptance", "handoff " + handoff, "contract " + contract,
             "review " + args.review, accepted]
    for text in args.rests_on:
        text = no_break(text, "--rests-on").strip()
        if not text:
            raise Refuse("--rests-on is empty")
        lines.append("rests-on " + text)
    return lines


def parse_note(text):
    """Returns the key lines of a note as (key, value) pairs."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip():
            if line.startswith("<!-- readback ") and line.rstrip().endswith("-->"):
                del lines[i]
            break
    pairs = []
    for line in lines:
        match = KEY_RE.match(line.rstrip("\r"))
        if match:
            pairs.append((match.group(1), match.group(2).strip()))
    return pairs


def check_note(text):
    findings = []
    values = {}
    twice = []
    for key, value in parse_note(text):
        if key in values and key != "rests-on":
            if key not in twice:
                twice.append(key)
                findings.append("key %s stands twice" % key)
            continue
        values.setdefault(key, value)
    kind = values.get("kind")
    if kind is None:
        findings.append("missing key kind")
    elif kind not in KEYS:
        findings.append("kind is neither review nor acceptance: %r" % kind)
    else:
        for key in KEYS[kind]:
            if key not in values:
                findings.append("missing key %s" % key)
    if "handoff" in values and not ID_RE["HND"].match(values["handoff"]):
        findings.append("handoff value is no HND id: %r" % values["handoff"])
    if "contract" in values and not ID_RE["CON"].match(values["contract"]):
        findings.append("contract value is no CON id: %r" % values["contract"])
    if "review" in values and values["review"] != "none" and not ID_RE["NOTE"].match(values["review"]):
        findings.append("review value is neither none nor a NOTE id: %r" % values["review"])
    if "JUDGE" in values:
        fields = values["JUDGE"].split(" | ")
        if fields[0].strip() not in JUDGE_WORDS:
            findings.append("JUDGE first field is none of PASS, FAIL, none, off, skipped: %r" % fields[0])
        if len(fields) < 4:
            findings.append("JUDGE has %d fields; it needs four separated by ' | '" % len(fields))
    if "ACCEPTED" in values:
        fields = values["ACCEPTED"].split(" | ", 2)
        answer = fields[0].strip()
        if not (answer.startswith("yes") or answer.startswith("no")):
            findings.append("ACCEPTED value starts with neither yes nor no: %r" % values["ACCEPTED"])
        elif answer.startswith("no") and (len(fields) < 3 or not fields[2].strip()):
            findings.append("ACCEPTED no has no reason")
        if len(fields) < 2 or not valid_date(fields[1].strip()):
            findings.append("ACCEPTED date is not YYYY-MM-DD: %r" % (fields[1].strip() if len(fields) > 1 else ""))
    return {"valid": not findings, "kind": kind, "findings": findings}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refuse(message)


def build_parser():
    parser = Parser(prog="closure_note.py", allow_abbrev=False, add_help=False)
    sub = parser.add_subparsers(dest="command", parser_class=Parser)
    review = sub.add_parser("review", allow_abbrev=False, add_help=False)
    for option in ("--handoff", "--contract", "--helper-output"):
        review.add_argument(option, required=True)
    acceptance = sub.add_parser("acceptance", allow_abbrev=False, add_help=False)
    for option in ("--handoff", "--contract", "--review", "--accepted", "--date"):
        acceptance.add_argument(option, required=True)
    acceptance.add_argument("--reason")
    acceptance.add_argument("--rests-on", action="append", default=[])
    check = sub.add_parser("check", allow_abbrev=False, add_help=False)
    check.add_argument("file")
    return parser


def main(argv):
    try:
        args = build_parser().parse_args(argv)
        if args.command == "review":
            out = "\n".join(make_review(args)) + "\n"
        elif args.command == "acceptance":
            out = "\n".join(make_acceptance(args)) + "\n"
        elif args.command == "check":
            result = check_note(read_text(args.file, "the note"))
            sys.stdout.write(json.dumps(result) + "\n")
            return 0 if result["valid"] else 1
        else:
            raise Refuse("the first argument is review, acceptance or check")
    except Refuse as err:
        sys.stderr.write(" ".join(str(err).split()) + "\n")
        return 2
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
