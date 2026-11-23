# Amendment 13

Status: the owner approved this work in DEC-20261123-07. No package ships this amendment yet; a release needs the owner's GO.

## Gap

On 2026-11-23 six of six handoffs written without a skeleton gave `unparsed` from the validator. Six of six trial handoffs and two real handoffs written from a skeleton gave `true`. A sub-agent report arrives with two spaces before each line: the JSON form survives this, the text form does not. The lead had no script that prints the form and no rule that puts it into the worker's prompt.

## Change

| Piece | Change |
|---|---|
| `scripts/handoff_skeleton.py` | Reads a contract through `validate_handoff.parse_contract` and prints one JSON object with the keys of PROTOCOL.md 3.3 in their order. `checks` holds one string for each distinct Check text in contract order: the check text, then the exit placeholder and the result placeholder, joined as in an evidence line. `contract` holds every ACC id with `<VERIFIED, FAILED or UNVERIFIED>`. The option `--text` prints the same content in the spaced key form. |
| Exit 2 | If the contract cannot be read, holds no statement, or holds a Check that the validator can never match (a line break, or no Check text), the script prints nothing on standard output, one line with the reason on standard error, and exits 2. |
| `output-styles/readback.md` section CONTRACT and PROTOCOL.md section 3.2 | Two sentences: one command in each Check, joined with `sh -c`; the lead puts the printed skeleton into the worker's prompt. |
| `output-styles/readback.md` section ACCEPT and PROTOCOL.md section 4 | Two sentences: the line `<!-- readback lead -->` above the JUDGE line of a stored handoff; the results of the joined run go to the worker before the judge call, and the judge reads the updated handoff. |
| `agents/worker.md` section When you stop | One sentence: the worker fills a given skeleton and returns it as its whole report. |
| `scripts/sync.py`, `portable/skills/` | The script is copied into the portable lead skill. The skills are regenerated. |
| `checks/test_handoff_skeleton.py`, `checks/test_texts_skeleton.py` | One test for each clause of the statements, and the named sections. |

## Limits

- The skeleton fixes the form of a handoff, not its truth. A filled skeleton can still be wrong in content.
- An evidence line that the worker adds by hand can still hold a wrong escape. In the JSON form a check text with the two characters `\n` stands as `\\n`; a filled skeleton in which `\\n` was changed to `\n` gives `unparsed` with a V6 finding. A test pins this.
- The text form does not survive a report whose lines have leading spaces. Use the JSON form for a sub-agent.
- The script adds no frame logic and does not read the publisher's envelope. That is the validator's part (CON-20261123-05).
- The script does not judge whether a Check is a good check. It refuses only a Check that cannot be matched.
