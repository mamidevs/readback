# Amendment 07

Status: approved by the owner on 2026-11-16. DEC-20261116-07. The text is v0.6 from this amendment.

The owner said: "tamam bunu uygula, codexde hakem fable claudeda hakem codex olsun, kullandigim cli'a gore otomatik secilsin"

| Gap | Change |
|---|---|
| Outside judging needed a table and worked from Claude Code only. | Codex leads select Claude Fable; Claude Code leads select Codex. Both use high effort by default. |
| Portable skills did not include the outside adapter. | The generated lead skill includes the Python helper and judge rules. |
| A deadline could allow a late answer. | The helper terminates the process group and never accepts an answer after its deadline. |
| The stored verdict could lose its reasoning. | The lead stores the full helper output, including execution metadata and the judge's answer. |
| A judge could inherit lead routing instructions. | Judge calls are terminal, receive explicit role instructions and reject nested helper calls. |

The helper uses Python's standard library. No routing service is required.
Optional teammate rows may adjust configuration but cannot choose the lead's provider or replace Fable with another model family.
Missing tools, invalid configuration and failed calls produce `JUDGE none`. They do not trigger another provider or model.
Legacy four-column rows still apply to their compatible lead host.

## Limits

- Claude Fable can read files but cannot run shell checks.
- Provider CLIs enforce the permissions. Managed policies and CLI runtime writes remain outside readback's control.
- Claude must report Fable model usage. Codex's requested model is recorded without independent model attestation.
- A commit label does not identify uncommitted changes.
- Resume graders, worker routing and usage-based model selection remain outside this amendment.
