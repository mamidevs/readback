# Amendment 14

Status: the owner approved this work in DEC-20261123-07. No package ships this amendment yet; a release needs the owner's GO.

## Gap

On 2026-11-23 the lead held 11 handoffs unstored until a verdict was known, because a stored record cannot get lines later. Five replacement chains were recorded as prose. On 2026-11-19 another project stored verdicts in free-form notes that a count read as unjudged handoffs. The protocol told the lead to add the judge call and the acceptance below the stored handoff, so a handoff could be stored only at the end.

## Change

| Piece | Change |
|---|---|
| `scripts/closure_note.py review` | Prints a review note: the lines `kind review`, `handoff <HND id>`, `contract <CON id>`, then the first three lines of the helper output unchanged. |
| `scripts/closure_note.py acceptance` | Prints an acceptance note: `kind acceptance`, `handoff`, `contract`, `review` (a NOTE id or `none`), `ACCEPTED <yes or no> | <date>` with ` | <reason>` when a reason is given, and one `rests-on` line for each `--rests-on`. |
| `scripts/closure_note.py check` | Reads one note, with or without the publisher's envelope line, and prints one JSON object with `valid`, `kind` and `findings`. Exit 0 for a valid note, exit 1 for a defect. |
| Exit 2 | A missing or malformed argument, `--accepted no` without a reason, or a file that cannot be read: nothing on standard output, one line with the reason on standard error. |
| `output-styles/readback.md` section ACCEPT and PROTOCOL.md section 4 | The lead stores the handoff when it arrives and adds no line below it. Each judge call is a review note, the acceptance an acceptance note. One sentence says that an older stored handoff holds the lead's lines below the worker's text. The review note holds the JUDGE, execution and receipt lines. |
| `skills/outside-judge/SKILL.md` step 4 | The review note holds the lines; the sentences about the lines below the handoff and ACCEPTED below the worker text are gone. |
| `scripts/sync.py`, `portable/skills/` | The script is copied into the portable lead skill. The skills are regenerated. |
| `checks/test_closure_note.py`, `checks/test_texts_closure.py` | One test for each clause of the statements, and the named sections. |

## Limits

- `check` reads the form of one note. It does not show that the review exists, that the judge said PASS, or that the acceptance is justified.
- The script does not store a note and does not count open work. Publication of notes is the publisher's part.
- Stored records stay as they are. The code block with the JUDGE and ACCEPTED lines in the ACCEPT sections still shows the form of those lines.
