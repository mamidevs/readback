# Amendment 06

Status: approved. DEC-20261116-04 and DEC-20261116-05. `PROTOCOL.md` includes row 2.
The text is v0.5 from this amendment.

The owner wants the same behavior in each CLI. Each row proposes one fix.

| # | Gap | Proposed text | Section |
|---|---|---|---|
| 1 | The lead rules load only through an output style of Claude Code. | `portable/skills/` holds the lead rules and the role rules as Agent Skills. `scripts/sync.py` writes them from the Claude Code files. In Codex one line in `AGENTS.md` loads the lead skill. | — |
| 2 | Each CLI keeps a memory of its own. The state of the work can spread over two hosts. | The records hold the state of the work. Do not write it to the memory of the host CLI. | 6 |
| 3 | No check shows the same behavior on a second host. | `checks/resume-codex.sh` runs the two-session resume check with Codex as the lead. | — |

## What the checks showed

- The resume check passes with Codex as the lead. The control without the skills loses the open task.
- The line in `AGENTS.md` loads a skill for a task that changes a file. It does not load it for a plain question.
- A Codex worker with the portable worker rules passed 11 of the 13 patterns of the worker evals. The 2 misses are a form: it wrote `"status": "FAILED"` in a nested object. Section 3.3 does not fix the form of a JSON value.

## Not in this amendment

- A fixed form for the values of a JSON handoff.
- A judge at another provider from a Codex lead.
- A worker or an explorer at another provider.
- A choice of provider from the usage limits.
