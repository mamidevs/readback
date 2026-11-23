# Amendment 15

Status: the lead gave this work as CON-20261123-09. No package ships this amendment yet; a release needs the owner's GO.

## Gap

On 2026-11-23 the lead rewrote the open work of one task block 15 times by hand. CON-20261123-05 replaced CON-20261123-04 and the only trace is prose in a LOG line. A withdrawn contract had no record. The open work was counted from memory and from prose, so a count and the records could differ without a trace.

Measured reasons: on 2026-11-23 the lead rewrote the open work of one task block 15 times by hand; CON-20261123-05 replaced CON-20261123-04 and the only trace is prose in a LOG line; in another project six free-form notes hold verdicts that a first count read as unjudged handoffs, and one of them names two handoffs that were never sent to the judge; of the 135 handoffs of this project that were stored at the time of the count, none holds `contract_id`, 62 name no contract id in their text and 21 name more than one.

## Change

| Piece | Change |
|---|---|
| `scripts/closure_note.py withdrawal` | Prints a withdrawal note: the lines `kind withdrawal`, `contract <CON id>`, `replaced-by <CON id or none>`, `date <date>`, `reason <text>`. A missing or malformed argument gives exit 2 with empty standard output and one line on standard error. |
| `scripts/closure_note.py check` | Reads the kind withdrawal. Defects: a missing key; a key that stands twice; a contract value that is no CON id; a `replaced-by` value that is neither `none` nor a CON id; `replaced-by` equal to `contract`; a date that is not YYYY-MM-DD; an empty reason. The rules for the kinds review and acceptance do not change. The finding for an unknown kind now names withdrawal. |
| `scripts/open_work.py --records <folder>` | Reads the records of one folder and prints one JSON object with `records`, `handoffs`, `contracts`, `unlinked`, `legacy_notes` and `conflicts`. It writes no file and changes no record. |
| Handoff states | `conflict`, `accepted`, `rejected`, `replaced`, `judged`, `open`; the first rule that applies gives the state. Decisions come from the embedded ACCEPTED line, from acceptance notes and from an attributed free-form note. A `no` decision never makes a handoff accepted. |
| Contract lists | `closed`, `withdrawn`, `open`, `unbound`. A handoff in the state `conflict` does not close its contract. |
| Conflicts | `decision`, `accepted-and-replaced`, `missing-target`, `contract-mismatch`, `withdrawn-and-closed`, `invalid-note`, `unreadable`. The script lists a conflict and resolves none. |
| Free-form notes | A note without a `kind` line is read by the legacy rule. A note whose first line starts with `#` and names one HND id is attributed to that handoff; any other free-form note with a JUDGE line is listed with `attributed` false and changes no state. |
| `output-styles/readback.md` section "CURRENT: after each brief" and PROTOCOL.md section 6 | One list item directly after the item that starts "After each BRIEF, publish OPEN and NEXT": take the open contracts and handoffs from `scripts/open_work.py`, and name its conflicts and its unlinked count with them. |
| `scripts/sync.py`, `portable/skills/` | `open_work.py` is copied into the portable lead skill. The skills are regenerated. |
| `checks/test_withdrawal_note.py`, `checks/test_open_work.py`, `checks/test_texts_open_work.py`, `checks/fixtures/open_work/` | One test for each clause and rule, two fixture folders with invented records, and the named sections. |

## Limits

- The script reads form and links; it does not show that a verdict is right.
- A contract without a linked handoff is `unbound`, not closed.
- Each CR run before an LF in a record is read as LF. A CR not before an LF stays, so a lead marker line that ends in a CR without an LF is not a marker.
- A record that is not UTF-8 text is counted in `records`, is listed as `unreadable` and stands in no other list.
- Stored records stay as they are. The script does not store a note and does not publish.
