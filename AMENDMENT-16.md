# Amendment 16

Status: contract CON-20261123-10. No package ships this amendment yet; a release needs the owner's GO.

## Gap

The measured reasons: on 2026-11-23 the lead paired every handoff with its contract by hand and ran the validator by hand; it stored 18 handoffs that day; a handoff did not name its contract in a key before validator 2.3.0. The lead had no one command that reported, before publication, whether each record of a request was well formed and whether its links led to a stored record.

## Change

| Piece | Change |
|---|---|
| `scripts/record_preflight.py --request <json file> --records <folder>` | Reads the list `records` of the request that `scripts/publish_records.py --request` reads, and prints one JSON object with `ok` and `records`. Each entry holds `id`, `kind`, `checked` and `defects`. The folder is optional; a record of the folder is the file `<id>.md`, and its envelope line is left out before use. A record that an id names is searched in the request first, then in the folder. |
| HND entry | `validation` is `unreadable`, `unbound`, `contract-missing`, `true`, `false <n>` or `unparsed`, from `scripts/validate_handoff.py`. `replaces` is `absent`, `none`, `found` or `missing`. Defects: `validation`, `contract-missing`, `replaces-missing`. |
| NOTE entry with the `kind` line `review` or `acceptance` | `check` is the result of `check_note` in `scripts/closure_note.py`; `links` lists the keys `handoff`, `contract` and `review` with their state. Defects: `check`, `link-missing`, `contract-mismatch`, `review-mismatch`. |
| Other records | `checked` false and no defect. A note kind that the script does not check, such as a later kind added to `scripts/closure_note.py`, is left alone. |
| Exit status | 0 when `ok` is true, 1 when it is false. 2 for a request that cannot be read or is malformed, a missing folder, a malformed argument, or a helper that cannot be loaded (that line starts with `unavailable:`); nothing on standard output, one line on standard error. |
| `output-styles/readback.md` section "Project settings: resolve the record destination first" | One sentence after the publisher sentence: "Run `scripts/record_preflight.py` on a request before you publish it. It reports and refuses nothing." |
| `scripts/sync.py`, `portable/skills/` | The script is copied into the portable lead skill. The skills are regenerated. |
| `checks/test_record_preflight.py`, `checks/test_texts_preflight.py` | One test for each clause of the statements and the Definitions, and the named section. |

## Place of the check

Codex reviewed the place of the check: inside the publisher it adds a recovery path and lock time for a result that refuses nothing; no skipped check and no changed retry result was measured. The script stands beside the publisher, and `scripts/publish_records.py` is not changed.

## Limits

- The script reports and refuses nothing; it stores nothing and changes no record.
- An exit status 0 does not show that a verdict is right or that an acceptance is justified.
- Nothing makes the lead run it.
