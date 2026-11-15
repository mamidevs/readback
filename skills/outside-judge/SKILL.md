---
name: outside-judge
description: Sends a HANDOFF and its CONTRACT to a judge at another provider and returns PASS, FAIL or no verdict. Use it after a worker stops, when ~/logbook/TEAMMATES.md names a judge.
---

# Outside judge

Use this skill only when `~/logbook/TEAMMATES.md` has a `judge` row.
Without that file or that row, use the judge agent of this host.

The other provider sees the contract, the handoff and the repository folder.

## Steps

1. Store the contract and the handoff as files. They are the stored records `records/<CON id>.md` and `records/<HND id>.md`.
2. Run this command. Do not change it.

```
"${CLAUDE_PLUGIN_ROOT}/scripts/outside-judge.sh" <contract file> <handoff file> <repository folder>
```

3. Read the first two lines of the output.

```
JUDGE      <PASS, FAIL or none> | <provider:model> | <repo@sha or none> | <note>
execution  <provider> | <model requested> | <effort requested> | cli | none
```

4. Add both lines and the ACCEPTED line below the stored handoff. Do not change the worker's text.

## Rules

- Exit code 2 means that no outside judge is named. Use the judge agent of this host.
- Exit code 3 means that the call gave no verdict. Write `JUDGE none`. Do not accept the work. Tell the owner.
- A FAIL never sends the handoff to another judge.
- Do not run the command again with another model.
- The first word of the judge's answer is the verdict. Words in the rest of the answer do not change it.
