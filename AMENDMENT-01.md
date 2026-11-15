# Amendment 01

Status: approved. DEC-20261115-04. `PROTOCOL.md` includes every row below.

The first use of the protocol showed four gaps. Each line below proposes one fix.

| # | Gap | Proposed text | Section |
|---|---|---|---|
| 1 | The DEC record has no line shape. | `DEC-<YYYYMMDD>-<NN> \| <project> \| <what was decided> \| "<owner's exact words>" \| <date>` | 3.1 |
| 2 | A bare GO can meet several open asks. | Number the open asks. A bare GO answers number 1 only. | 2 |
| 3 | `ref` has no value when the project has no commit. | Write `doc:<path>` when no commit exists. | 3.4 |
| 4 | A task can have no contract. The readback, the `contract` key and DONE all need one. | Without a contract, write `done when:` in plain words and `contract: none`. DONE then means that each check is VERIFIED. | 2, 3.3 |

The first records used the shape in row 1. They wrote `-` in `ref` before row 3 was approved.

## Rules in the adapter files that have no source in PROTOCOL.md

The files under `.claude/` copy the protocol text. These six rules are new. Rows 3 and 4 change behavior.

| # | File | Rule |
|---|---|---|
| 1 | output style | Do not translate fixed words, ids, code or commands. |
| 2 | output style | Answer a question in 1 to 3 sentences when you can. A question gets no BRIEF. |
| 3 | output style | The project's instructions say which actions need approval. |
| 4 | output style | If the project names no place for records, show each record line to the owner. |
| 5 | explorer, judge | The agent changes no file. For an explorer, `changes` and `commit` are `none`. |
| 6 | judge | After the first word, name each claim that has no evidence and each hidden failure. |
