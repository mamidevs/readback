# Amendment 10

Status: the owner approved this work on 2026-11-17 in DEC-20261117-23, DEC-20261117-24 and DEC-20261117-28. It is part 1 of the settings and judge redesign. The core rules stay Readback Protocol v0.7. No package ships this amendment yet; a release needs the owner's GO.

The owner approved the review receipt as a safety net on 2026-11-17: "onayliyorum" (DEC-20261117-24). The owner approved the five parts and their order on 2026-11-17: "onayliyorum, A grubuna gecelim" (DEC-20261117-28).

| Piece | Gap | Change |
|---|---|---|
| Structural validator, `scripts/validate_handoff.py` | A handoff with a missing key, a wrong key order or an unresolved line reached the judge unchecked. | The script reads a contract and a handoff, runs nothing from them and prints one JSON object with the findings and `valid`. |
| Content capture, `scripts/review_capture.py` | A commit label does not identify uncommitted changes in the reviewed checkout. | A successful capture identifies the bytes and selected metadata of tracked and non-ignored untracked paths listed by Git. It is not an atomic snapshot. |
| Review records, `scripts/review_receipt.py` | No stored record showed what a judge run looked at. | The module stores a started record and a completed record below the Git common directory and builds the receipt line. |
| Commit observation, `scripts/commit_observation.py` | Nothing showed whether a commit followed a judge run on the same bytes. | When enabled, commit observation attempts to record one line for a command that the guard does not deny and that contains a recognized commit call or a failed-parse candidate. The line describes the first recognized commit call in traversal order, or records a parse failure. Matching review records are listed when capture and lookup succeed. |
| Helper hardening in `scripts/outside_judge.py` | SIGTERM did not interrupt the provider call, a failing repository lookup could stop the helper, and the helper could write bytecode. | SIGTERM interrupts the call. A failing lookup gives a receipt of none and the call goes on. The helper writes no bytecode. |

The helper prints the receipt as its third line, after the verdict line and the execution line. A receipt changes no verdict. PROTOCOL.md section 4 holds both forms.

## Measurements

Each result file below is a mutation run of one script with its own tests. The line below each file is the result line of that run.

`validate_handoff.py`: file `~/logbook/readback/records/settings-design-20261117/mutation-validate_handoff-run1-lead.json`, sha256 `5e281f1f387a7110e8918b1647336b7dbdec9bf67af42b633fc46d59c37318a8`.

```
mutation result: sites 345 | killed 285 (of these by a time limit: 0) | survivors 60 | skipped 0 | baseline exits [0, 0]
```

`review_capture.py`: file `~/logbook/readback/records/settings-design-20261117/mutation-review_capture-run3-worker.json`, sha256 `4da676b0584f6e1cfdb6bf4f2dbe50529ba566c9d03fd72757aedfc8814891cf`.

```
mutation result: sites 121 | killed 120 | survivors 0 | skipped 1 | baseline exits [0, 0, 0]
```

`review_receipt.py`: file `~/logbook/readback/records/settings-design-20261117/mutation-review_receipt-run2-lead.json`, sha256 `4677c2272bed85ac2b75c9c9b842816f6f75a0b3890df302daa5446d5068e0fc`.

```
mutation result: sites 83 | killed 83 | survivors 0 | skipped 0 | baseline exits [0, 0]
```

`commit_observation.py`: file `~/logbook/readback/records/settings-design-20261117/mutation-commit_observation-run2-lead.json`, sha256 `d8690885a821bd0a81173d74e8c81cb9317095228ba2291e5081b7d2da022aae`.

```
mutation result: sites 203 | killed 203 (of these by a time limit: 0) | survivors 0 | skipped 0 | baseline exits [0, 0]
```

The validator tests leave 60 of 345 mutants alive. In part 1 the validation result is recorded and blocks nothing. Part 2 hardens the validator before its result blocks anything (part-1-spec.md J13, in `~/logbook/readback/records/settings-design-20261117/`).
No mutation result is pinned here for `scripts/outside_judge.py`.

The offline suite ran 536 tests at the last change of this amendment; all passed and none was skipped.

## Limits

- The review records and the observation file are local and untrusted: the agent runs as the same user and can write them.
- ends-equal means that both captures succeeded with the same manifest identity; it does not establish unchanged content between captures.
- A stored completed record includes the structural validation result or an unavailable status; validation changes no verdict and blocks nothing in part 1.
- The commit observation describes; it denies nothing and decides nothing.
- The hook reads the command line before the Bash call starts; it does not follow cd or resolve Git aliases.
- The hook locates substitution boundaries without interpreting quotes or heredocs; it then tokenizes extracted text with shlex.
- The judge still reads the live folder.
