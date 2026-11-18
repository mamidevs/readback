# Amendment 11

Status: the owner approved this work in DEC-20261117-23, DEC-20261117-28 and DEC-20261118-01. It is part 2 of the settings and judge redesign. The rules are Readback Protocol v0.8 from this amendment on. No package ships this amendment yet; a release needs the owner's GO.

Part 1 gave the outside judge a receipt. Part 2 lets the owner choose how often the outside judge runs, and it counts the rounds of a judged piece. PROTOCOL.md section 4 and the ACCEPT section of the output style hold the acceptance rules that follow from the mode.

| Piece | Gap | Change |
|---|---|---|
| Settings, `scripts/readback_settings.py` and `schemas/settings.schema.json` | The judge route, the mode and the round limit had no local settings file. | The script reads and writes `settings.json` in the project home with the commands show, set and migrate-teammates. The schema holds the shape of the file. |
| Session state, `scripts/readback_session.py` | A mode choice of the owner had no place to stay between prompts. | The module keeps the mode override of one session and the owner's round authorizations in local state files. |
| Structural validator 2.0.0, `scripts/validate_handoff.py` | The validator of part 1 did not read the sections that a contract holds after `Out of scope:` or a handoff key that ends with a colon, and its result blocked nothing. | Version 2.0.0 reads those sections (Limits, Definitions, Evidence requirements, Coverage list, Command lines) and the keys with a colon. The helper uses the result: false or unavailable blocks a provider call, and an acceptance without a judge needs true. |
| Judge line, `hooks/judge_line.py` | The owner had no short way to change the mode or to authorize a round. | The hook reads a judge line at UserPromptSubmit. At SessionEnd, and at SessionStart with the source resume or fork, it clears the session state. |
| Commit observation switch, `hooks/guard_git.py` | The commit observation had only an environment variable as its switch, and an agent could change the judge settings with a command. | The setting `commit_check` with the value `off` turns the commit observation off for a project; `READBACK_COMMIT_CHECK=off` stays. The guard denies an agent's command that changes the judge settings or touches the session state. The observation describes and never blocks. |
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

- `JUDGE off|gate|each [session]` sets the mode of this session. The word `session` is optional and changes nothing.
- `JUDGE default` clears the stored override.
- `JUDGE round <CON id> <step|gate>` authorizes one round.

The mode of a project is the setting `judge.mode` in its settings file. A judge line does not change that file; it stores an override for one session.

The owner writes a judge line as the first line of a prompt.
A stored mode override lasts until another judge line replaces it or a cleanup removes it; JUDGE default clears it.
A stored round authorization permits one matching reservation.
A successful cleanup at SessionStart with the source resume or fork clears the override and the unused authorizations.
An agent never writes a judge line and never asks a tool to write one.

## Version

This package keeps protocol_version 0.7 in project profiles and in the registry; part 4 of the redesign owns the compatibility in scripts/project_registry.py, the profile, project and registry schemas, and the tests that keep 0.7 projects readable without a silent rewrite.

The tests of the texts of this amendment are in `checks/test_texts_part2.py`. They show that the named sentences and forms stand in the named files.

## Measurements

The lead ran these mutation checks at commit 0323483179f1d08493c5edbca941db8f8b6ba15b with `mutate.py` (sha256 9936de2d08bae1d8314aa17948431cbd3dc4556216725d7b8cdb8a55bdd63383), each on a fresh copy with an empty HOME and TMPDIR. The result files are in `~/logbook/readback/records/settings-design-20261117/part-2-measurements-0323483/`.

- `scripts/readback_settings.py` | commit 0323483 | `settings.json` sha256 8c7ad97a220b64d752de722e713426ca2719be2532eaa46bee9a7199175ed81e | `mutation result: sites 166 | killed 166 (of these by a time limit: 0) | survivors 0 | skipped 0 | baseline exits [0, 0]`
- `scripts/readback_session.py` | commit 0323483 | `session.json` sha256 a3a7ecc85f0f8ebdc1fa632769645deee94cce6f437df27084f029d92a0f9b1b | `mutation result: sites 140 | killed 140 (of these by a time limit: 0) | survivors 0 | skipped 0 | baseline exits [0, 0]`
- `scripts/validate_handoff.py` | commit 0323483 | `validator.json` sha256 8227a1cd37093582cab65ef93d7f334fa76f0fc700ce723460a34171e9e26079 | `mutation result: sites 390 | killed 390 (of these by a time limit: 0) | survivors 0 | skipped 0 | baseline exits [0, 0]`
- `hooks/judge_line.py` | commit 0323483 | `line.json` sha256 92d5caf0e37f600a7a3d970a99c76bf7d57a4b12d87fbf540c2fc4a850be9abb | `mutation result: sites 77 | killed 72 (of these by a time limit: 0) | survivors 5 | skipped 0 | baseline exits [0, 0]`
- `scripts/judge_rounds.py` | commit 0323483 | `roundsall.json` sha256 d4fb757d1482d23070f8736ce370691870cad3052e9239c4bb6a88ccbf94bdea | `mutation result: sites 261 | killed 257 (of these by a time limit: 0) | survivors 4 | skipped 0 | baseline exits [0, 0]`

The run of `scripts/judge_rounds.py` uses the test classes of `checks/test_judge_modes.py`, `checks/test_judge_rounds.py` and `checks/test_judge_retry.py` through the adapter `part2_helper_all.py` (sha256 90c234b172d3104894b94b81f3902083c8459b6c7b4c91e5d8dce3a79d38696f).
With `checks/test_judge_rounds.py` alone the same module gives 78 survivors, nearly all in the retry code that `checks/test_judge_retry.py` tests (`rounds.json`).
The five survivors of `hooks/judge_line.py` (lines 31, 109, 113, 122 and 126) are the equivalent mutants that the lead ruled on in HND-20261118-16.
The four survivors of `scripts/judge_rounds.py` are equivalent: line 173 only adds calls of `_make_dirs` for folders that exist, line 294 changes a pending attempt in memory that is not published, and both mutants of line 518 end the watchdog without a kill, as the original does for an empty pipe.

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
