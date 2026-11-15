# Amendment 02

Status: approved. DEC-20261115-07. `PROTOCOL.md` includes every row below.

The first package showed four gaps. Each row proposes one fix.

| # | Gap | Proposed text | Section |
|---|---|---|---|
| 1 | The worker has no stop rule for an action that needs approval. | If the task needs an action that needs approval, the worker stops. It returns BLOCKED and names the action. | 3.3 |
| 2 | A worker cannot always read the project's instructions. It needs a default list. | Without a list from the project, five actions need approval. They are a push, a deploy, a write to a shared database, a delete outside the task and a new item on an external service. | 3.1 |
| 3 | Section 3 names the main session "harness" in three rows and "lead" in one. | Use "lead" in all four rows. | 3 |
| 4 | `<project>` is a folder name. Two repositories with the same folder name share one log. | When two projects have the same name, write `<parent folder>-<project>`. | 3.4 |
