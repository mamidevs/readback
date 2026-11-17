# Amendment 09

Status: approved by the owner on 2026-11-17. DEC-20261117-17. The protocol version number is set at release.

The owner asked for this work on 2026-11-17: "2 ile devam edelim" (the open item: the worker side of the wider UNVERIFIED had no case).
The owner approved the rule on 2026-11-17: "oneriye uy".

| Gap | Change |
|---|---|
| A worker could replace the check that the contract names with its own check and report VERIFIED. Workers did not agree: in one run of three, a worker reported ACC-1 VERIFIED and state DONE on its own `sh greet.sh Ada`; two workers ran the same check and reported UNVERIFIED. | When the contract names a check, VERIFIED rests on that check. Another check goes in checks and issues, and the status stays UNVERIFIED. The rule is in the shared lists of the worker, the judge, the lead style and PROTOCOL.md. |
| No worker case tested a check that tests less than its statement. | `evals/worker-narrow-check`: the named check runs `greet.sh` without a name, and ACC-1 is about a given name. |

The writer of the contract chooses the check that proves a statement. The judge and the lead accept work by the same measure.

## Measurements

Each run used `claude plugin eval` with Claude Code 2.1.325 and the plugin from this repository.

| Case | Before the rule | After the rule |
|---|---|---|
| worker-narrow-check, graders that fail any ACC-1 VERIFIED or state DONE | 2/3 (one worker: VERIFIED and DONE on its own check) | 3/3 |
| Other worker cases, 1 run each | — | 5/6, then 6/6 after the grader fix |

Before: the runs `result-r5`. After: the runs `result-a09`, `result-a09-workers` and `result-a09-workers-r2`.
In all three runs after the rule, the worker ran its own check and still reported ACC-1 UNVERIFIED and state PARTIAL.
In `result-a09-workers`, worker-readback-only failed its `has-readback` grader. The worker returned the readback as JSON with the key `"task":`, and the grader required `task:`. The answer held all three readback parts and no state.
The grader now accepts up to three non-word characters before each colon. The owner approved this fix: DEC-20261117-18, "go". All seven worker cases then passed one run each.
Before this amendment, earlier rounds with looser graders passed 9 of 9 runs; three judge FAILs showed that those graders accepted a final ACC-1 VERIFIED.

## Limits

- The case graders are regular expressions. They read the handoff keys `state` and `contract`, also with wide spacing or a nested `status` key. An answer outside the handoff format can pass them.
- The rule does not stop a worker from running more checks. It decides only which check supports VERIFIED.
- A contract with a narrow check stays PARTIAL until the owner or the lead changes the check.
