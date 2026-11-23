#!/usr/bin/env python3
"""Lead-controlled run of named checks on a clean copy of one commit.

Usage: trusted_run.py [--repo PATH] --commit REV --out DIR [--contract FILE ...] [-- COMMAND ...]

It adds a detached checkout of REV below DIR, gives every command a fresh HOME and TMPDIR, runs it with
`/bin/sh -ec` in the checkout, keeps each output and report.json in DIR, and prints the same report as
JSON. Each distinct Check command of each --contract file runs first, in contract order; COMMANDs follow.
The checkout is removed at the end, also after SIGTERM or SIGINT. Exit: 0 all commands gave 0, 1 one
failed, 2 a malformed argument or nothing to run (nothing on standard output, one line on standard error).
"""
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


class Stop(BaseException):
    def __init__(self, signum):
        self.signum = signum


def usage_error(message):
    print("trusted_run.py: " + message, file=sys.stderr)
    raise SystemExit(2)


def parse(argv):
    opts = {"repo": os.path.dirname(HERE), "commit": None, "out": None, "contracts": []}
    names = {"--repo": "repo", "--commit": "commit", "--out": "out", "--contract": "contracts"}
    i = 0
    while i < len(argv) and argv[i] != "--":
        if argv[i] not in names or i + 1 >= len(argv) or argv[i + 1] == "--":
            usage_error("malformed argument: " + argv[i])
        if names[argv[i]] == "contracts":
            opts["contracts"].append(argv[i + 1])
        else:
            opts[names[argv[i]]] = argv[i + 1]
        i += 2
    opts["commands"] = argv[i + 1:]
    if not opts["commit"] or not opts["out"]:
        usage_error("--commit and --out are required")
    return opts


def contract_commands(paths):
    commands = []
    if paths:
        sys.path.insert(0, HERE)
        from validate_handoff import parse_contract
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                parsed = parse_contract(handle.read())
        except (OSError, UnicodeDecodeError) as err:
            usage_error("cannot read contract %s: %s" % (path, err.__class__.__name__))
        if not parsed["ok"] or not parsed["statements"]:
            usage_error("contract %s has no statements" % path)
        for statement in parsed["statements"]:
            if not (statement["check"] or "").strip():
                usage_error("%s has no Check in %s" % (statement["id"], path))
            if statement["check"] not in commands:
                commands.append(statement["check"])
    return commands


def on_signal(signum, _frame):
    raise Stop(signum)


def main(argv):
    opts = parse(argv)
    repo = os.path.abspath(opts["repo"])
    commands = contract_commands(opts["contracts"]) + opts["commands"]
    if not commands:
        usage_error("no command to run")
    found = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q", opts["commit"] + "^{commit}"],
                           capture_output=True, text=True)
    if found.returncode != 0:
        usage_error("not a commit of %s: %s" % (repo, opts["commit"]))
    sha = found.stdout.strip()
    outdir = os.path.abspath(opts["out"])
    checkout = os.path.join(outdir, "checkout")
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, on_signal)
    results, status, child, added = [], "", None, False
    try:
        os.makedirs(outdir, exist_ok=True)
        added = True
        subprocess.run(["git", "-C", repo, "worktree", "add", "--detach", "-q", checkout, sha], check=True)
        for index, command in enumerate(commands, 1):
            home = os.path.join(outdir, f"home-{index}")
            tmp = os.path.join(outdir, f"tmp-{index}")
            os.makedirs(home)
            os.makedirs(tmp)
            env = {"HOME": home, "TMPDIR": tmp, "PATH": os.environ["PATH"], "LANG": "en_US.UTF-8",
                   "PYTHONDONTWRITEBYTECODE": "1"}
            started = time.time()
            child = subprocess.Popen(["/bin/sh", "-ec", command], cwd=checkout, env=env, start_new_session=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            data = child.communicate()[0]
            code, child = child.returncode, None
            path = os.path.join(outdir, f"output-{index}.txt")
            with open(path, "wb") as handle:
                handle.write(data)
            results.append({"command": command, "exit": code, "seconds": round(time.time() - started, 1),
                            "output": path, "sha256": hashlib.sha256(data).hexdigest(),
                            "tail": data.decode("utf-8", "replace").strip().splitlines()[-3:]})
        status = subprocess.run(["git", "-C", checkout, "status", "--porcelain"], capture_output=True, text=True).stdout
    except Stop as stop:
        signal.signal(stop.signum, signal.SIG_IGN)
        return 128 + stop.signum
    finally:
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, signal.SIG_IGN)
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            child.wait()
        if added:
            subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", checkout], capture_output=True)
            subprocess.run(["git", "-C", repo, "worktree", "prune"], capture_output=True)
    report = {"repository": repo, "commit": sha, "results": results, "tree_changes_after_run": status.splitlines()}
    with open(os.path.join(outdir, "report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1)
    print(json.dumps(report, indent=1))
    return 0 if all(r["exit"] == 0 for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
