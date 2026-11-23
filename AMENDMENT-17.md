# Amendment 17

Status: contract CON-20261123-11. No package ships this amendment yet; a release needs the owner's GO.

## Gap

The measured reasons: the lead typed the Check commands of each contract by hand for about 25 runs of its own script; two stopped runs left their detached checkout behind; a contract of that day holds a Check with a semicolon chain, where `sh -c` reports the exit status of the last command only.

## Change

| Piece | Change |
|---|---|
| `scripts/trusted_run.py --commit <rev> --out <dir> [--repo <path>] [--contract <file>] [-- <command> ...]` | The lead's runner is now in the repository. It runs each command with `/bin/sh -ec` in a detached checkout of the commit with a fresh HOME and TMPDIR, writes each output and `report.json` to the folder, and prints the report. Exit 0 when every command gave 0, 1 otherwise, 2 for a malformed argument. |
| `--contract` | May stand more than once. The script runs each distinct Check command once, in contract order (`parse_contract` of `scripts/validate_handoff.py`), then the commands after `--`. A statement without a Check, or no command at all, runs nothing and gives exit 2. |
| Cleanup | The checkout is removed at the end, also after SIGTERM or SIGINT. The working tree of the repository is not changed. |
| `output-styles/readback.md` ACCEPT, `PROTOCOL.md` section 4 | One sentence after the clean-checkout sentence: "`scripts/trusted_run.py` is such a script for checks that are shell commands." |
| `scripts/sync.py`, `portable/skills/` | The script is copied into the portable lead skill by its own statement; the helper tuple is unchanged. |
| `checks/test_trusted_run.py` | One test for each clause of the statements and the Definitions. |

## Limits

- Nothing makes the lead use the script; a project CI run is still allowed.
- The script runs shell commands of a contract; it does not judge their result.
