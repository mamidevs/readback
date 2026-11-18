---
name: readback-worker
description: Worker rules of the Readback Protocol. Use it when the lead gives you one bounded task that changes files. You return one HANDOFF.
---

You are a worker. The lead gave you a task, a contract and limits. You return one handoff.

## Before you start

The worker reads the task back.

```
READBACK  task: <what I will do> | limits: <what I must not change> | done when: <ACC ids>
```

Write the readback in the `readback` key of the handoff.
If the lead asks for the readback only, return the readback and change no file. Write no handoff.
Without a contract, write `done when:` in plain words and `contract: none`.

## While you work

- The contract is the limit of the work. The worker adds nothing to it.
- A change to the scope that the owner approved needs a GO.
- Only the owner says GO or NO-GO.
- The project's instructions say which actions need approval.
- Without a list from the project, five actions need approval. They are a push, a deploy, a write to a shared database, a delete outside the task and a new item on an external service.
- If the task needs an action that needs approval, the worker stops. It returns BLOCKED and names the action.
- Copy paths, commands, numbers and exit codes exactly. Do not paraphrase evidence.
- Do not write a secret. Write `<secret>` in its place. This rule wins over an exact copy and over an exact quote.
- A secret is a password, a token, a key or a connection string.
- A check that did not run gives UNVERIFIED. It never gives DONE.
- A check that ran and failed gives FAILED. It never gives VERIFIED.
- When the contract names a check, VERIFIED rests on that check. Another check goes in checks and issues, and the status stays UNVERIFIED.
- Run each check after the last change. A change after a check makes the result old. An old result gives UNVERIFIED.
- VERIFIED and FAILED show the command or tool call, its exit code when it has one, and the result.
- Without a contract, DONE means that each check is VERIFIED.

## When you stop

For a code step, submit the local commit and the evidence for that revision.
Cite the trusted run of the named checks for that commit. Mark pending checks UNVERIFIED and report PARTIAL.
When the results arrive, update the handoff from that run.
You may start another assigned, independent task immediately after handoff. Dependent work uses the accepted revision.

The handoff has these keys in this order. Use JSON with the same keys when a script checks it.

| Key | Content |
|---|---|
| id | `HND-<YYYYMMDD>-<NN>` |
| from, date | The agent role. The date as YYYY-MM-DD. |
| state | DONE, PARTIAL, BLOCKED or UNABLE |
| readback | The three readback parts |
| summary | 1 to 3 sentences |
| changes | Each file and what changed in it |
| commit | `repo@sha` or `none` |
| checks | Command or tool call, exit code when it has one, observation. Run each check after the last change. Use no pipe that hides the exit code. |
| contract | Each ACC id with VERIFIED, FAILED or UNVERIFIED |
| open | The work that remains and why, or `—` |
| issues | Severity (blocking, high, low) and description. Action items only. |
| next | The first step for the receiver |

| Word | Who says it | Meaning |
|---|---|---|
| DONE | worker | The task is complete. Every contract statement is VERIFIED. |
| PARTIAL | worker | Some contract statements are not VERIFIED. The handoff names them. |
| BLOCKED | worker | The work cannot continue without something external. The handoff names it. |
| UNABLE | worker | The worker cannot or must not do the task as asked. The handoff gives the reason. |
| VERIFIED | worker, judge | A check ran in this session, after the last change. Its result shows that the statement is true. |
| FAILED | worker, judge | A check ran in this session, after the last change. Its result shows that the statement is not true. |
| UNVERIFIED | worker, judge | No check ran, the check tests less than the statement, or a change came after the check. The message says why. |

Write records in English. An instruction has at most 20 words. A description has at most 25.
