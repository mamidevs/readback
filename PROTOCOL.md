# Readback Protocol v0.1

Status: approved. DEC-20261115-01, the owner said "go" on 2026-11-15.
This text includes Amendment 01 (DEC-20261115-04) and Amendment 02 (DEC-20261115-07).

One format for what the harness says and what it records.
The owner reads less and misses nothing.

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
| R6 | Write records in English. An instruction has at most 20 words. A description has at most 25. | LOG, CONTRACT, HANDOFF, DEC |
| R7 | Speak to the owner in the owner's language. A field has at most 2 lines. A list has at most 5 items. | BRIEF, DECISION |
| R8 | Do not translate fixed words, ids, code or commands. | all messages |

## 2. Fixed words

| Word | Who says it | Meaning |
|---|---|---|
| DONE | worker | The task is complete. Every contract statement is VERIFIED. |
| PARTIAL | worker | Some contract statements are not VERIFIED. The handoff names them. |
| BLOCKED | worker | The work cannot continue without something external. The handoff names it. |
| UNABLE | worker | The worker cannot or must not do the task as asked. The handoff gives the reason. |
| VERIFIED | worker, judge | A check ran in this session. The message shows the result. |
| UNVERIFIED | worker, judge | No check ran. The message says why. |
| PASS | judge | The evidence supports every claim in the handoff. |
| FAIL | judge | A claim has no evidence, or the handoff hides a failure. |
| GO | owner | The owner approves this one decision. |
| NO-GO | owner | The owner rejects this one decision. |

- A check that did not run gives UNVERIFIED. It never gives DONE.
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
DEC-<YYYYMMDD>-<NN> | <project> | <what was decided> | "<owner's exact words>" | <date>
```

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

The worker reads the task back before it starts.

```
READBACK  task: <what I will do> | limits: <what I must not change> | done when: <ACC ids>
```

The lead corrects a wrong readback.
If the host cannot carry a reply, the worker writes the readback in the `readback` key of the handoff.
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
| checks | Command, exit code, observation. Use no pipe that hides the exit code. |
| contract | Each ACC id with VERIFIED or UNVERIFIED |
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
KNOW       <risks, surprises and UNVERIFIED items>
OPEN       <the work that remains>
Next:      <one step>
```

- Quote the owner's decisions in the owner's words.
- Mark a decision that the agent made with `[agent decision]`.
- Labels in other languages map one to one. Turkish: Yapılan, Senden beklenen, Bilmen gereken, Yapılmayan, Sonraki adım.

## 4. Independence

The agent that did the work does not judge it.
The judge is a different agent. Use a different provider if possible.
The judge checks three things:

1. The evidence supports each claim.
2. The handoff hides no failure.
3. Each contract statement has a status.

The first word of the judge's answer is PASS or FAIL.
After the first word, name each claim that has no evidence and each failure that the handoff hides.

## 5. Trace

- Each LOG line cites its approval in `go` and its proof in `ref`.
- Each ACC statement has a status in the `contract` key of a HANDOFF.
- Each DEC record quotes the owner. It does not translate the quote.
