# Amendment 04

Status: approved. DEC-20261115-15. `PROTOCOL.md` includes every row below.
Row 3 changes the DEC line. For that reason the text is v0.3 from this amendment.

A second outside review looked at v0.2 as a way to carry work across sessions. Each row proposes one fix.

| # | Gap | Proposed text | Section |
|---|---|---|---|
| 1 | No record holds the open work of the lead. Without a worker no handoff exists. The open work stays in the conversation. | The lead keeps one file for each project: `CURRENT.md`. It holds the goal, the standing decisions, the open work and one next step. The lead writes it again after each BRIEF. | 6 |
| 2 | The text says how to write records. It does not say how a new session reads them. | When the owner asks to continue, the lead reads CURRENT.md, the newer LOG and DEC lines and the named handoff. It compares them with the repository. The repository wins. | 6 |
| 3 | A DEC line does not say if it approves one action or records a standing decision. Nothing marks a decision that a later one replaced. | The DEC line ends with a kind (`once` or `standing`) and with the id that it supersedes. A DEC line without a kind approves no action now. | 3.1 |
| 4 | A new session can read an old approval as a GO. | A `once` decision from an earlier session approves nothing now. A `standing` decision is not an approval for a later action. | 3.1, 6 |
| 5 | The judge's answer stays in the conversation. The records do not show if a judge ran. | The lead adds a JUDGE line and an ACCEPTED line below the stored handoff. It does not change the worker's text. | 4, 5 |
| 6 | Every ACC statement can be VERIFIED while the owner's request is not met. | Before the lead accepts, it reads the owner's request again. | 4 |
| 7 | A record can hold a guess about the owner as if the owner said it. | A DEC records what the owner said. Do not record a guess about what the owner wants. | 3.1 |
| 8 | No check shows that a new session finds the open work. | `checks/resume.sh` runs two sessions. The second session must find an open task that no file shows. | — |

## Not in this amendment

- A store for the owner's preferences across projects. The CLI's own instruction files hold them today.
- An approval that carries to a new session. Row 4 says the opposite.
- A scope field on the DEC line. The project field is the scope.
