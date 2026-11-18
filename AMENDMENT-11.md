# Amendment 11

Status: the owner approved this work in DEC-20261117-23, DEC-20261117-28 and DEC-20261118-01. It is part 2 of the settings and judge redesign. The rules are Readback Protocol v0.8 from this amendment on. No package ships this amendment yet; a release needs the owner's GO.

Part 1 gave the outside judge a receipt. Part 2 lets the owner choose how often the outside judge runs, and it counts the rounds of a judged piece. PROTOCOL.md section 4 and the ACCEPT section of the output style hold the acceptance rules that follow from the mode.

| Piece | Gap | Change |
|---|---|---|
| Settings, `scripts/readback_settings.py` and `schemas/settings.schema.json` | The judge route, the mode and the round limit had no local settings file. | The script reads and writes `settings.json` in the project home with the commands show, set and migrate-teammates. The schema holds the shape of the file. |
| Session state, `scripts/readback_session.py` | A mode choice of the owner had no place to stay between prompts. | The module keeps the mode override of one session and the owner's round authorizations in local state files. |
| Structural validator 2.0.0, `scripts/validate_handoff.py` | The validation result of part 1 blocked nothing. | Version 2.0.0 hardens the validator. Its result now decides whether a provider call runs and whether an acceptance without a judge stands. |
| Judge line, `hooks/judge_line.py` | The owner had no short way to change the mode or to authorize a round. | The hook reads a judge line at UserPromptSubmit, SessionStart and SessionEnd and keeps the session state in step. |
| Commit observation switch, `hooks/guard_git.py` | The commit observation needs a switch that the owner can set. | `READBACK_COMMIT_CHECK=off` in the Git guard turns the commit observation off. The observation describes and never blocks. |
| Helper modes, `scripts/outside_judge.py` | The helper called a provider for every handoff. | The helper reads the mode and the tier. In the mode off and in a step handoff of the mode gate it prints JUDGE off or JUDGE skipped and makes no provider call. |
| Round accounting, `scripts/judge_rounds.py` | Nothing limited the number of judge rounds for one piece. | The module keeps one ledger for each project, repository, lineage and tier. The limit is judge.max_fail_rounds. |
| Legacy home of a linked worktree, `scripts/project_registry.py` | A linked worktree could not find the legacy home of its main worktree. | The registry gives a linked worktree the legacy home that is named after the folder of the main worktree. |

## Modes

The judge runs in one of three modes. The mode decides which handoff gets an outside judge.

| Mode | Step handoff | Gate handoff |
|---|---|---|
| off | no judge; JUDGE off | no judge; JUDGE off |
| gate | no judge; JUDGE skipped | outside judge |
| each | outside judge; a step PASS accepts nothing | outside judge |

The default mode is gate.

A step handoff is the handoff of one piece. A gate handoff is the handoff of a part. The helper takes the tier from the option `--tier`.

## Owner channel

The owner changes the mode and authorizes a round with a judge line. There are three forms.

- `JUDGE off|gate|each [session]` sets the mode.
- `JUDGE default` clears the stored override.
- `JUDGE round <CON id> <step|gate>` authorizes one round.

The owner writes a judge line as the first line of a prompt.
A stored mode override lasts until another judge line replaces it or a cleanup removes it; JUDGE default clears it.
A stored round authorization permits one matching reservation.
A successful cleanup at SessionStart with the source resume or fork clears the override and the unused authorizations.
An agent never writes a judge line and never asks a tool to write one.

## Version

This package keeps protocol_version 0.7 in project profiles and in the registry; part 4 of the redesign owns the compatibility in scripts/project_registry.py, the profile, project and registry schemas, and the tests that keep 0.7 projects readable without a silent rewrite.

The tests of the texts of this amendment are in `checks/test_texts_part2.py`. They show that the named sentences and forms stand in the named files.

## Limits

- Every protection of this amendment is a safety net: the agent runs as the same user, can write the files, edit the hooks and call a provider directly.
- Local records do not authenticate who wrote them.
- JUDGE off and JUDGE skipped end with exit status 0; read the word after JUDGE, never the exit status alone.
- A record of off or skipped binds no content and gives no eligibility for a commit.
- The commit check observes and never blocks in this part.
- Sampled Claude hand-backs begin with framing; the event fields do not authenticate the owner as the author.
- Under Codex the hook behaviour and the session variable are documented, not measured.
- A settings file that exists hides the legacy table; migrate-teammates shows the mapping, and --write fills only the settings leaves that are missing.
- Within one project, repository, lineage and tier, the FAIL verdicts and the pending attempts fill judge.max_fail_rounds slots, 3 by default.
- A new lineage starts a new count.
- The helper needs the git command; without it the project cannot be resolved and the helper gives no verdict.
