# Amendment 03

Status: approved. DEC-20261115-12. `PROTOCOL.md` includes every row below.
Rows 1 and 2 change the meaning of two fixed words. For that reason the text is v0.2 from this amendment.

An outside review of v0.1 found gaps in the status words, the readback, the records and the proof of behavior. Each row proposes one fix.

| # | Gap | Proposed text | Section |
|---|---|---|---|
| 1 | VERIFIED means that a check ran. A check that ran and failed has no word, and DONE can follow it. | VERIFIED: A check ran in this session, after the last change. Its result shows that the statement is true. FAILED: the same, and the statement is not true. A check that ran and failed gives FAILED. It never gives VERIFIED. | 2, 3.3 |
| 2 | A result can be older than the files that it describes. | A change after a check makes the result old. An old result gives UNVERIFIED. The judge runs the check again when it can. | 2, 3.3, 4 |
| 3 | The text says that the worker reads back before it starts. In the default form the readback arrives with the handoff. | The readback has two forms. The recorded readback finds a wrong start after the work. The confirmed readback stops a wrong start. Use the confirmed readback when a wrong start is hard to undo. | 3.3 |
| 4 | R5 and the DEC quote copy text exactly. A secret in that text goes to a record. | R9: Do not write a secret. Write `<secret>` in its place. This rule wins over R5 and over an exact quote. | 1 |
| 5 | R7 limits a field to 2 lines and a list to 5 items. More items can exist. The text said that the owner misses nothing. | R10: When a limit cuts items, put first the items that need the owner. Write how many remain and where they are. | 1, 3.5 |
| 6 | A LOG line can cite an HND id that no file holds. | The lead stores each CONTRACT and each HANDOFF. One record is one file. Cite an HND id only when the lead stored that handoff. | 3.4, 5 |
| 7 | PASS is about the handoff. The text did not say when the lead accepts the work, or what follows a FAIL. | The lead accepts the work when the state is DONE and the judge says PASS. After a FAIL, the lead sends the work back or tells the owner what failed. | 4 |
| 8 | No check shows that an agent follows the protocol. | The repository holds eval cases for the worker and the judge. The plugin has a version. | — |

## Not in this amendment

- A hash of the changes that a check covered. Row 2 covers the common case.
- A DEC id in the CONTRACT and in the HANDOFF. The worker stops before an action that needs approval.
- Ids that stay unique across machines. A shared store gives out the ids.
