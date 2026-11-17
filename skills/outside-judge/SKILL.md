---
name: outside-judge
description: Sends a HANDOFF and its CONTRACT from Claude Code to a Codex judge automatically. Returns PASS, FAIL or no verdict.
---

# Outside judge

Use this skill after a worker stops. Claude Code selects Codex automatically.
The default judge is `gpt-6-astra` at `high` effort. No teammates table is required.

The other provider sees the contract, the handoff and the repository folder.

## Steps

1. Store the contract and the handoff as files. They are the stored records `records/<CON id>.md` and `records/<HND id>.md`.
2. Run this command. Do not change it.

```
bash "${CLAUDE_PLUGIN_ROOT}/scripts/outside-judge.sh" --host claude <contract file> <handoff file> <repository folder>
```

3. Read the complete output. Its first three lines identify the verdict, the execution and the receipt.

```
JUDGE      <PASS, FAIL or none> | <provider:model> | <repo@sha or none> | <note>
execution  <provider> | <model requested> | <effort requested> | cli | none
receipt    <attempt id> | <ends-equal or unbound: reason> | validation <true, false n, unparsed or unavailable> | <path of the completed file>
receipt    none | <reason>
```

The helper prints one of the two receipt forms.

The third line is the receipt. A receipt changes no verdict.

4. Store the complete output below the handoff, including the judge's reasoning. Then add ACCEPTED. Do not change the worker's text.

## Rules

- Exit code 3 means that the call gave no verdict. Write `JUDGE none`. Do not accept the work. Tell the owner.
- Missing tools, invalid configuration and a missed deadline give no verdict. Do not substitute a local judge.
- A FAIL never sends the handoff to another judge.
- Do not run the command again with another model.
- The first word of the judge's answer is the verdict. Words in the rest of the answer do not change it.
