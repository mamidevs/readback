# Amendment 05

Status: approved. DEC-20261116-01. `PROTOCOL.md` includes every row below.
The text is v0.4 from this amendment.

Section 4 says: "Use a different provider if possible." The package had no way to do it.
The owner and two models agreed on a first step: a judge at another provider. Each row proposes one rule.

| # | Gap | Proposed text | Section |
|---|---|---|---|
| 1 | Every agent runs at the provider of the lead. | A teammate can run at another provider than the lead. Sections 2 to 6 stay the same for that teammate. | 7 |
| 2 | Another provider sees the task. The owner must choose that. | Use a teammate at another provider only when the owner's local table names it. | 7 |
| 3 | Two hosts can give out the same record id. | The lead gives out the record ids before it delegates. Only the lead writes records. | 7 |
| 4 | A change of provider can loosen a limit. | A change of provider does not change the contract, the checks, the approvals or the permissions. | 7 |
| 5 | A call to another provider can end without an answer. | A call that fails gives no verdict. Write `JUDGE none`. The lead does not accept the work. | 7 |
| 6 | A lead can look for a judge that says PASS. | A FAIL never sends the handoff to another judge. | 7 |
| 7 | The records do not show which provider, model and effort ran. | The lead adds an `execution` line below the stored handoff. It holds the requested values. | 7 |
| 8 | No check shows that the call works. | `checks/outside-judge.sh` runs the two judge fixtures through the call and tests a missed deadline. | — |

## Not in this amendment

- A worker or an explorer at another provider.
- A fallback to another model when a limit is reached.
- A choice of provider from the usage limits.
- A lead on another CLI.
- An advisor role. The owner said that the providers have one.
