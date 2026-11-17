#!/usr/bin/env python3
"""Print one line per entry below ROOT/judge-*/work, for the live judge check.

Run: python3 checks/judge_snapshot.py ROOT

A line is `<path relative to ROOT> <value>`. The value is the sha256 of the bytes of a file,
`link:<target>` for a symlink and `directory` for anything else. The folder `.git/readback` of a work
folder and everything below it are left out: the judge helper stores its review records there.
A path that cannot be read stops the command with a non-zero exit.
"""

import hashlib
from pathlib import Path
import sys


def snapshot(root):
    lines = []
    for folder in sorted(root.glob("judge-*/work")):
        for path in sorted(folder.rglob("*")):
            if path.relative_to(folder).parts[:2] == (".git", "readback"):
                continue
            if path.is_symlink():
                value = "link:" + str(path.readlink())
            elif path.is_file():
                value = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                value = "directory"
            lines.append(str(path.relative_to(root)) + " " + value)
    return lines


if __name__ == "__main__":
    for line in snapshot(Path(sys.argv[1])):
        print(line)
