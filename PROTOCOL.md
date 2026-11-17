# Readback Protocol v0.6

Status: approved. DEC-20261115-01, the owner said "go" on 2026-11-15.
This text includes Amendment 01 (DEC-20261115-04), Amendment 02 (DEC-20261115-07), Amendment 03 (DEC-20261115-12), Amendment 04 (DEC-20261115-15), Amendment 05 (DEC-20261116-01), Amendment 06 (DEC-20261116-04, DEC-20261116-05) and Amendment 07 (DEC-20261116-07).
Amendment 03 changed the meaning of two fixed words. Amendment 04 changed the DEC line. Amendment 05 added teammates at other providers. Amendment 06 added the rule about the memory of the host. Amendment 07 made opposite-provider judging automatic. Each change gave the text a new number.

One format for what the lead says and what it records.
The owner reads less. A message that leaves items out says how many and where they are.

Adapted from ideas in ASD-STE100, EARS, ICAO radiotelephony, S1000D and DO-178C.
It does not conform to any of them.

## 1. Language rules

| # | Rule | Applies to |
|---|---|---|
| R1 | Put the result first. Write no preamble, no recap and no closing line. | all messages |
| R2 | Write one fact or one instruction in each sentence. | all messages |
| R3 | Use the active voice. Name who did the action. | all messages |
| R4 | Use one word for one meaning. Use each fixed word only as section 2 defines it. | all messages |
| R5 | Copy paths, commands, numbers and exit codes exactly. Do not paraphrase evidence. | all messages |
| R6 | Write records in English. An instruction has at most 20 words. A description has at most 25. | LOG, CONTRACT, HANDOFF, DEC, CURRENT |
| R7 | Speak to the owner in the owner's language. A field has at most 2 lines. A list has at most 5 items. | BRIEF, DECISION |
| R8 | Do not translate fixed words, ids, code or commands. | all messages |
| R9 | Do not write a secret. Write `<secret>` in its place. This rule wins over R5 and over an exact quote. | all messages |
| R10 | When a limit cuts items, put first the items that need the owner. Write how many remain and where they are. | BRIEF, DECISION |

A secret is a password, a token, a key or a connection string.

## 2. Fixed words

| Word | Who says it | Meaning |
|---|---|---|
| DONE | worker | The task is complete. Every contract statement is VERIFIED. |
| PARTIAL | worker | Some contract statements are not VERIFIED. The handoff names them. |
| BLOCKED | worker | The work cannot continue without something external. The handoff names it. |
| UNABLE | worker | The worker cannot or must not do the task as asked. The handoff gives the reason. |
| VERIFIED | worker, judge | A check ran in this session, after the last change. Its result shows that the statement is true. |
| FAILED | worker, judge | A check ran in this session, after the last change. Its result shows that the statement is not true. |
| UNVERIFIED | worker, judge | No check ran, or a change came after the check. The message says why. |
| PASS | judge | The evidence supports every claim in the handoff. |
| FAIL | judge | A claim has no evidence, or the handoff hides a failure. |
| GO | owner | The owner approves this one decision. |
| NO-GO | owner | The owner rejects this one decision. |

- A check that did not run gives UNVERIFIED. It never gives DONE.
- A check that ran and failed gives FAILED. It never gives VERIFIED.
- A change after a check makes the result old. An old result gives UNVERIFIED.
- VERIFIED and FAILED show the command, the exit code and the result.
- Only the owner says GO or NO-GO.
- A GO covers one decision. It does not carry to the next decision.
- Number the open asks. A bare GO answers number 1 only.
- Without a contract, DONE means that each check is VERIFIED.

## 3. Message types

| Type | From, to | When |
|---|---|---|
| DECISION | lead to owner | An action needs the owner's approval. |
| CONTRACT | lead to worker | Before work that needs more than one step. |
| HANDOFF | worker to lead | The worker stops. |
| LOG | lead to record | Something that outlives the session changed. |
| BRIEF | lead to owner | A task that changed something ends. |

Each record has an id: `<TYPE>-<YYYYMMDD>-<NN>`. The types are DEC, CON, HND and LOG. An id is never used again.
Read the stored records to find the next number.

### 3.1 DECISION

The project's instructions say which actions need approval.
Without a list from the project, five actions need approval. They are a push, a deploy, a write to a shared database, a delete outside the task and a new item on an external service.

```
DECISION   <one question that the owner can answer with GO or NO-GO>
RECOMMEND  <one option and one reason>
COST       <what a GO changes, and what is hard to undo>
OTHERS     <at most 2 alternatives, one line each>
IF GO      <the first action>
```

Record the answer as `DEC-<YYYYMMDD>-<NN>` with the owner's exact words and the date.

```
DEC-<YYYYMMDD>-<NN> | <project> | <what was decided> | "<owner's exact words>" | <date> | <kind> | <supersedes>
```

- kind: `once` or `standing`
- `once`: the owner approved one action. The approval ends when the action ran or the session ends.
- `standing`: the owner decided how something is or how to work. It stays in force until a later DEC supersedes it.
- A `standing` decision is not an approval for a later action. An action that needs approval needs its own GO.
- If you cannot tell the kind, write `once` and ask the owner.
- supersedes: the id of the DEC that this decision replaces, or `-`. Do not edit the old line.
- A DEC line without a kind is from an older version. It approves no action now. A choice that it records stays in force until a later DEC supersedes it.
- A DEC records what the owner said. Do not record a guess about what the owner wants.

Labels in other languages map one to one. Turkish: Karar, Öneri, Bedel, Diğer seçenek, GO olursa.

### 3.2 CONTRACT

Write the contract before the work. Use 1 to 5 statements. A one-line fix needs none.

```
CON-<YYYYMMDD>-<NN>  <the task in one sentence>
ACC-1  The <thing> shall <response>.                                Check: <command or observation>
ACC-2  When <trigger>, the <thing> shall <response>.                Check: ...
ACC-3  While <state>, the <thing> shall <response>.                 Check: ...
ACC-4  If <unwanted condition>, then the <thing> shall <response>.  Check: ...
Out of scope: <what the worker must not change>
```

The contract is the limit of the work. The worker adds nothing to it.
A change to the scope that the owner approved needs a GO.

### 3.3 HANDOFF

The worker reads the task back.

```
READBACK  task: <what I will do> | limits: <what I must not change> | done when: <ACC ids>
```

The readback has two forms.

- Recorded readback. The worker writes the readback in the `readback` key of the handoff. The lead compares it with the contract before it accepts the work. This form finds a wrong start after the work.
- Confirmed readback. The lead asks for the readback only. The worker returns the readback and changes no file. The lead corrects it, then starts the work. This form stops a wrong start.

The recorded readback is the default. Use the confirmed readback when a wrong start is hard to undo.
Without a contract, write `done when:` in plain words and `contract: none`.
If the task needs an action that needs approval, the worker stops. It returns BLOCKED and names the action.

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
| checks | Command, exit code, observation. Run each check after the last change. Use no pipe that hides the exit code. |
| contract | Each ACC id with VERIFIED, FAILED or UNVERIFIED |
| open | The work that remains and why, or `—` |
| issues | Severity (blocking, high, low) and description. Action items only. |
| next | The first step for the receiver |

A project profile can add keys. It cannot remove or rename these keys.
An explorer and a judge change no file. For an explorer, `changes` and `commit` are `none`.

### 3.4 LOG

Write one line for each change. Only add lines. Do not edit old lines.

```
LOG-<YYYYMMDD>-<NN> | <project> | <target> | <what changed> | <why> | <go> | <ref>
```

- target: `code:<repo>@<sha>`, `server:<host>`, `db:<name>`, `config:<path>` or `doc:<path>`
- go: the DEC id and the owner's exact words. Write `-` when the change needed no approval.
- ref: the commit, the HND id or the check that proves the change
- Cite an HND id only when the lead stored that handoff.
- Write `doc:<path>` when no commit exists.
- When two projects have the same name, write `<parent folder>-<project>`.

If the project names a place for records, write them there. If it names none, show each record line to the owner.

### 3.5 BRIEF

Answer a question in 1 to 3 sentences when you can. A question gets no BRIEF.

The brief has four fields in this order, then one next step.
Write `—` in an empty field. Do not omit the field.

```
DID        <what changed, in plain words>
NEEDS YOU  <decisions and approvals that wait for the owner>
KNOW       <risks, surprises, FAILED and UNVERIFIED items>
OPEN       <the work that remains>
Next:      <one step>
```

- Quote the owner's decisions in the owner's words.
- Mark a decision that the agent made with `[agent decision]`.
- If a field has more items than fit, write how many remain and where they are. Example: `+3 more in HND-20261115-02`.
- When a worker did the work and no judge ran, say so in KNOW.
- Labels in other languages map one to one. Turkish: Yapılan, Senden beklenen, Bilmen gereken, Yapılmayan, Sonraki adım.

## 4. Independence

The agent that did the work does not judge it.
The judge is a different agent. Use a different provider if possible.
The supported lead integrations select the other provider automatically:

| Lead | Judge | Default model | Effort |
|---|---|---|---|
| Codex | Claude Code | `fable` | high |
| Claude Code | Codex | `gpt-6-astra` | high |

No teammates table is required. The lead names its host when it calls the helper.
An unavailable provider gives `JUDGE none`. Do not substitute a local judge or another model.
The judge does not delegate or start another judge.

The judge checks four things:

1. The evidence supports each claim.
2. The handoff hides no failure.
3. Each contract statement has a status.
4. Each VERIFIED status has a check that ran after the last change. Run the check again when you can.

The first word of the judge's answer is PASS or FAIL.
After the first word, name each claim that has no evidence and each failure that the handoff hides.

PASS is about the handoff. It does not say that the task is complete.
A handoff that reports FAILED or UNVERIFIED with evidence can get PASS.

The lead accepts the work when the state is DONE and the judge says PASS.
After a FAIL, the lead sends the work back or tells the owner what failed.
If the state is not DONE, the lead tells the owner what remains.
Before the lead accepts, it reads the owner's request again. If the contract missed a part of the request, the lead tells the owner.

The lead adds two lines below the stored handoff. It does not change the worker's text.

```
JUDGE     <PASS, FAIL or none> | <the judge> | <repo@sha or none> | <what the judge found, or —>
ACCEPTED  <yes or no> | <date> | <the reason, when no>
```

For an outside judge, store the complete helper output before ACCEPTED, including the execution line and the full answer.
The output records the provider, requested model, reported model when available, requested effort and execution method.
Claude Fable receives only Read, Grep and Glob tools. It cannot rerun shell checks.
Codex runs with its read-only sandbox. CLI settings and managed policies remain part of the trust boundary.
A commit label does not identify uncommitted changes in the reviewed checkout.

## 5. Trace

- Each LOG line cites its approval in `go` and its proof in `ref`.
- Each ACC statement has a status in the `contract` key of a HANDOFF.
- Each DEC record quotes the owner. It does not translate the quote.
- The lead stores each CONTRACT and each HANDOFF in the place for records. One record is one file. The file name is the id.
- A `ref` points to something that the reader can open.
- Each stored handoff carries the judge's answer and the lead's acceptance.

## 6. Resume

A new session does not know the last one. The records carry the work.
The lead keeps one file for each project in the place for records: `CURRENT.md`.

```
CURRENT   <project>
updated   <YYYY-MM-DD> | after <the last LOG or DEC id that this file covers>
GOAL      <what the owner wants from this project now>
IN FORCE  <each standing decision that matters now: the DEC id and 3 to 8 words>
OPEN      <the work that remains and the asks that wait for the owner>
NEXT      <one step>
```

- Write CURRENT.md again after each BRIEF. Replace the old text. The LOG stays add-only.
- CURRENT.md has at most 30 lines. It names records by id. It does not copy them.
- CURRENT.md is a summary. If it disagrees with a record, the record wins.
- Do not write a secret in CURRENT.md.
- The records hold the state of the work. Do not write it to the memory of the host CLI.

When the owner asks to continue earlier work, do these steps before the work.

1. Read CURRENT.md.
2. Read the LOG and DEC lines that are newer than the id in `updated`.
3. Read the stored handoff that an open item names.
4. Compare with the repository: the branch, the last commit and the changes that are not committed.
5. Tell the owner the goal, the open work and the next step in at most 3 lines.

- Start the next step only if it needs no answer from the owner.
- If the records and the repository disagree, the repository wins. Tell the owner what differs.
- A `once` decision from an earlier session approves nothing now. Ask again.
- If no CURRENT.md exists, say so. Read the last 20 lines of the LOG.
- If the owner does not name the project, use the project of the session's folder. If it has no records, ask which project.

## 7. Teammates at other providers

A teammate can run at another provider than the lead. Sections 2 to 6 stay the same for that teammate.

- Judges follow the automatic routing in section 4. Their local table is optional.
- Other roles use another provider only when the owner's local table names it.
- The lead gives out the record ids before it delegates. Only the lead writes records.
- A change of provider does not change the contract, the checks, the approvals or the permissions.
- A teammate gets only the context that its contract needs.
- A call that fails gives no verdict. Write `JUDGE none`. The lead does not accept the work.
- A FAIL never sends the handoff to another judge.

The lead adds one line below the stored handoff for each teammate at another provider.

```
execution  <provider> | <model requested> | <effort requested> | <transport> | <run id or none>
```

Write the requested values. Write the model that the host reports only when it differs from the request.

## Optional project binding extension (package v0.7.0)

A project may explicitly bind several repositories to one local record home. The resolved home overrides the repository-name default. An unbound repository retains the default; a marked but invalid binding stops writes. Shared records retain repository and check-context provenance. Only the lead publishes records through the profile's serialized publisher. A generated index locates documents and never overrides them. See [shared projects](docs/shared-projects.md). Package v0.7.0 includes this extension. The core communication rules above remain v0.6.
