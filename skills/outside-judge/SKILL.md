---
name: outside-judge
description: Sends a HANDOFF and its CONTRACT from Claude Code to a Codex judge automatically. Returns PASS, FAIL or no verdict.
---

# Outside judge

Use this skill after a worker stops. Claude Code selects Codex automatically.
The default judge is `gpt-6-astra` at `high` effort. No teammates table is required.

The other provider sees the contract, the handoff and the repository folder.
The mode of the judge decides whether a provider call runs. In the mode off, and in a step handoff of the mode gate, the helper makes no provider call.

## Steps

1. Store the contract and the handoff as files. They are the stored records `records/<CON id>.md` and `records/<HND id>.md`.
2. Run one of these commands. Use the first for one piece and the second for the handoff of a part. Do not change the rest.

```
bash "${CLAUDE_PLUGIN_ROOT}/scripts/outside-judge.sh" --host claude --tier step <contract file> <handoff file> <repository folder>
bash "${CLAUDE_PLUGIN_ROOT}/scripts/outside-judge.sh" --host claude --tier gate --coverage <coverage file> <contract file> <handoff file> <repository folder>
```

The complete form of the helper's arguments is this.

```
outside-judge.sh --host <host> [--tier step|gate] [--lineage <CON id>] [--coverage <file>] <contract file> <handoff file> <repository folder>
```

Pass --tier step for one piece and --tier gate for the handoff of a part.
The option `--lineage` names the CON id that the rounds of one piece are counted under.
The option `--coverage` names a file that lists the worker contracts and the original handoffs of a gate call.

3. Read the complete output. Its first three lines identify the verdict, the execution and the receipt.

```
JUDGE      <PASS, FAIL or none> | <provider:model> | <repo@sha or none> | <note>
execution  <provider> | <model requested> | <effort requested> | cli | none
receipt    <attempt id> | <ends-equal or unbound: reason> | validation <true, false n, unparsed or unavailable> | <path of the completed file>
receipt    none | <reason>
```

The helper prints one of the two receipt forms.

The third line is the receipt. A receipt changes no verdict.

The first line takes one of five forms.

```
JUDGE      PASS | <provider:model> | <repo@sha or none> | <note>
JUDGE      FAIL | <provider:model> | <repo@sha or none> | <note>
JUDGE      none | <provider:model> | <repo@sha or none> | no verdict: <reason>
JUDGE      off | none:none | <repo@sha or none> | judge mode off (<source>); no provider call<tail>
JUDGE      skipped | none:none | <repo@sha or none> | judge mode gate skips a step review (<source>); no provider call<tail>
```

`<tail>` is empty for validation true; otherwise it is `; checks-only acceptance is not established: <validation phrase>`.
The second and third line of `off` and `skipped` are these, or the second form of the receipt when no record was stored:

```
execution  none | none | none | cli | none
receipt    <attempt id> | no review | <validation phrase> | <path>
receipt    none | <reason>
```

4. The review note holds its JUDGE, execution and receipt lines. Summarize any finding needed for ACCEPTED without sensitive details. The completed receipt keeps the detailed answer for 30 days.

## Rules

- Exit code 3 means that the call gave no verdict. Write `JUDGE none`. Do not accept the work. Tell the owner.
- When the host refuses the judge call before the helper starts, write `JUDGE none | <provider:model> | <repo@sha or none> | no verdict: host refusal: <the host's reason>`. Do not send the content another way. Ask the owner for a DECISION.
- Exit code 0 with JUDGE off or JUDGE skipped is not a verdict.
- After the checks of the arguments, the settings, the session and the inputs, structural validation runs before the mode decides; false or unavailable blocks a provider call, and unparsed goes to the judge with its findings.
- Missing tools, invalid configuration and a missed deadline give no verdict. Do not substitute a local judge.
- A FAIL never sends the handoff to another judge.
- Do not run the command again with another model.
- Do not change the mode, a route or a deadline to get another answer.
- The first word of the judge's answer is the verdict. Words in the rest of the answer do not change it.
