<img src="app/menu-bar/Assets/Readback.svg" width="88" height="88" alt="">

# Readback

Readback helps you coordinate coding work across Claude Code and OpenAI Codex. Give the lead a task, let workers continue independently, and review what passed on the combined revision. A local logbook keeps the decisions, evidence and next step available to the next session.

On macOS, the menu bar panel lets you choose a project's judge model and reasoning effort for each host. Open a project session in the panel to see what the agent has recorded.

The `main` branch includes the menu bar app and Readback Protocol v0.8 through Amendment 11. The `v0.9.0` tag is an earlier snapshot. Use `main` for the features described below.

## What it does

| Need | Readback's response |
|---|---|
| Keep the owner's decisions separate from agent assumptions | Record the owner's exact words. A GO applies to one decision. |
| Delegate without losing the task boundary | Give the worker a CONTRACT and receive a HANDOFF against it. |
| Distinguish a result from a claim | Mark checks VERIFIED, FAILED, or UNVERIFIED, with evidence. |
| Review work independently | Send the handoff to a judge at the other provider. Keep the full verdict. |
| Resume without reconstructing the conversation | Read a short `CURRENT.md`, then the records it references. |
| Coordinate related repositories | Resolve their shared project home and retain repository-specific evidence. |

readback organizes a personal agent workflow. It does not provide a hosted team memory service, an autonomous task scheduler, or a replacement for the model's own context system.

## The working loop

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/flow-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/flow-light.svg">
  <img alt="A task passes between the owner, lead, worker, independent judge, and logbook." src="docs/flow-light.svg">
</picture>

The lead gives each worker a bounded contract. A worker records its checks, hands off the result and can continue with the next independent task. The lead reviews exceptions as they arise. Work that depends on an unresolved result waits for it.

At the end of a section, the lead brings the work together and tests the requested behavior on the combined revision. In the default review mode, the other provider judges that evidence once for the section. A judge can return PASS on an accurate partial report. Closing the section also requires the checks to cover the requested behavior and pass.

You receive a short brief after work that changed something:

```text
DID        Login now limits failed attempts. You approved five tries per minute.
NEEDS YOU  —
KNOW       The local checks passed. Production behavior remains UNVERIFIED.
OPEN       Production deployment.
Next:      Review the deployment separately.
```

Labels follow your language; quotations and fixed status words retain their meaning. Plain questions can receive a short answer without a brief. [EXAMPLE.md](EXAMPLE.md) shows a complete task; [PROTOCOL.md](PROTOCOL.md) defines the messages and rules.

## macOS menu bar

From a checkout of `main` on macOS 14 or later with the Apple Command Line Tools installed, build and open the panel once:

```sh
app/menu-bar/build.sh && open app/menu-bar/build/Readback.app
```

1. Click the return-arrow icon in the menu bar and select a project.
2. Choose **Bölüm sonunda** for a section-end review or **Her teslimde** to also review individual handoffs. Open **Hakem modelleri**, select the host, set the judge model and effort, then choose **Kaydet**. Each host reads its own route from that project's settings. In **Her teslimde**, step and section-end reviews have separate routes.
3. Open **Proje oturumları** and select a Claude Code or Codex session. Its detail view refreshes while open and shows recorded operations. For Claude Code Mode, this includes child tool calls and their results when the transcript contains them.

For later launches, open the built `Readback.app`. See the [panel installation guide](INSTALL.md#macos-menu-bar) for rebuilds and session status labels.

## One repository or one project

An unbound repository uses `~/logbook/<repository-name>/`. Its records stay outside the code checkout:

```text
~/logbook/my-project/
  CURRENT.md        # Shared goal and decisions, plus open work and next step per task
  LOG.md            # Append-only change and decision history
  records/          # Contracts, handoffs, and judge decisions
```

For a project with several repositories, explicit settings bind them to one home:

```text
api/       ─┐
backend/   ─┼── project registry ── ~/logbook/my-project/
frontend/  ─┘
```

The private registry lives at `~/logbook/.readback/projects.json`. Each live bound repository has a `.readback-project.json` marker. The home adds project identity and profile files; its profile supplies project-specific policies without replacing the core protocol.

Both host integrations resolve the actual working directory before reading or writing project records. They show the repository, project, and selected home. They do not change the working directory. A handoff must identify its repository, worktree, branch, commit, and check directory; a test in the backend does not prove the frontend works.

Bindings must be configured explicitly. `STAGED` permits inspection; `ACTIVE` enables shared routing after validation. A copied or moved marked checkout needs a rebind. Missing, conflicting, or invalid bindings stop shared record writes instead of silently creating another logbook. Explicit compatibility fallback remains available for unmarked, unbound repositories.

There is no setup wizard. Read [project identity](docs/project-identity.md) for the registry, markers, profiles, validation, and activation requirements. [Shared projects](docs/shared-projects.md) covers operation and migration boundaries.

### Publishing shared records

Only the lead publishes records; workers return handoffs. The Markdown publisher checks the registry generation and merges each task's OPEN and NEXT under a lock. Sessions working on different tasks can publish from the same starting document. The publisher rejects stale updates to the same block and duplicate record IDs. Records declare shared scope or specific repository UUIDs.

Publication writes a durable journal, record files, LOG, and then CURRENT. Separate files are **not one atomic transaction**. An interruption blocks new publications until the saved operation is retried or its conflict is reconciled. The journal retains the previous CURRENT; it is not a full backup system.

The publisher requires Python 3, Git, and POSIX file locking. Locks coordinate record publication only; they do not lock code work or prevent an unrestricted process from editing files directly. No database or server is required.

### Finding the relevant documents

The optional index catalogs explicitly selected Markdown and text directories. It records paths, headings, line numbers, hashes, and literal repository mentions in `.readback/project-index.json` and `PROJECT-INDEX.md`.

A repository name ranks matching documents first. It never hides documents merely because they mention another repository. Queries search paths and document bodies, disclose counts and pagination, and report a stale saved index while scanning current inputs. Body matches include the matching line number; path-only matches have no matching line number. The lead rebuilds the saved index explicitly.

This helps an agent choose what to read. It does not promise a measured speedup: queries still inspect current files. The source document and current evidence remain authoritative. There are no embeddings, vector database, inferred ownership graph, or semantic search. Exclusions skip unsupported or unsafe file types and locations; filename filters are not a complete secret detector.

## Install

Automatic judging needs Python 3, both provider CLIs, and a login for each provider. Shared-project helpers additionally require Git and POSIX locking; their validation has run on macOS. Other operating systems remain unverified.

### Claude Code

Install the marketplace plugin and create the local logbook:

```sh
claude plugin marketplace add mamidevs/readback
claude plugin install readback@readback
mkdir -p ~/logbook
```

Then select the style in your Claude session:

```text
/output-style readback:Readback
```

The marketplace commands install the published plugin. To try a local checkout without installing it, run `claude --plugin-dir .` at its root and select the style in that session. For an existing installation, follow the [update instructions](INSTALL.md#update) and restart Claude Code.

### OpenAI Codex

The Codex integration uses the portable skills and a line in your global `AGENTS.md`:

```sh
git clone https://github.com/mamidevs/readback
mkdir -p ~/.agents/skills ~/.codex ~/logbook
cp -R readback/portable/skills/* ~/.agents/skills/
printf '\nFor every task, and when I ask to continue earlier work, load the skill `readback` and follow it.\n' >> ~/.codex/AGENTS.md
```

Run this setup once. The clone uses `main`; check out `v0.9.0` before copying only if you need that earlier release. To use an existing checkout, copy its `portable/skills/` folders into `~/.agents/skills/`, including the bundled scripts. For a checkout tracking `main`, pull and copy again. For a pinned release, select the newer tag and copy again. Start a fresh session after updating.

[INSTALL.md](INSTALL.md) covers installation checks, permissions, updates, and removal. Installing instructions does not grant access to your logbook; your CLI may ask for that separately.

## A judge at the other provider

| Lead | Outside judge | Default model | Effort |
|---|---|---|---|
| Codex | Claude Code | `fable` | high |
| Claude Code | Codex | `gpt-6-astra` | high |

The judge runs in one of three modes: off, gate and each. The default mode is gate: a step gets no outside judge, and a part gets one. The mode each also sends each step to the outside judge. The owner sets the mode with a judge line; [AMENDMENT-11.md](AMENDMENT-11.md) holds the forms.

The settings file is <project home>/settings.json. The script `scripts/readback_settings.py` reads and writes it:

```sh
python3 scripts/readback_settings.py --host <host> show
python3 scripts/readback_settings.py --host <host> set <key> <value>
python3 scripts/readback_settings.py --host <host> migrate-teammates
```

The integration selects the judge automatically. No teammates file is required. [TEAMMATES.example.md](TEAMMATES.example.md) describes optional overrides of the legacy table; a route from that table must keep a Codex lead's judge in the Fable family. A settings file sets the routes in `judge.routes` instead.

The judge receives the contract, the handoff and the reviewed folder's Git HEAD, status and diff statistics, without file contents. It can read the supplied review files through its CLI. **Judging sends task context to the other model provider.** Local storage does not make model calls local. Choose the review scope with that in mind.

Calls have a deadline. An unavailable model or failed call gives `JUDGE none`; there is no silent fallback. A FAIL goes back for correction and review by the same provider. The lead records the verdict, execution details and receipt reference. The receipt holds the masked response and becomes eligible for cleanup after 30 days. See [review record maintenance](INSTALL.md#review-record-maintenance).

Fable has Read, Grep, and Glob tools only; it cannot rerun shell checks. The Codex judge uses a read-only sandbox. Provider runtime restrictions remain the provider's responsibility. Execution metadata records the request; it is not independent model attestation.

The helper prints a receipt as its third line. A receipt changes no verdict. The helper stores review records below the Git common directory: `<git common dir>/readback/reviews/` holds the started and completed records, and `<git common dir>/readback/by-manifest/` holds markers for completed records whose second capture succeeded; a failed marker write leaves the completed record without a marker. The review records and the observation file are local and untrusted: the agent runs as the same user and can write them.

When enabled, commit observation attempts to append one line to `<git common dir>/readback/commit-observations.jsonl` for a command that the guard does not deny and that contains a recognized commit call or a failed-parse candidate. The line describes the first recognized commit call in the parser's traversal order, or records a parse failure. The commit observation describes; it denies nothing and decides nothing. Without a directory at `<git common dir>/readback/reviews`, the commit observation takes no capture and writes no observation. Set READBACK_COMMIT_CHECK=off to turn the commit observation off.

## Checks and limits

Run the offline checks from the repository root:

```sh
python3 scripts/sync.py --check
python3 -m unittest discover -s checks -p 'test_*.py' -v
git diff --check
```

The suites cover judge routing, project identity and fallback, publication conflicts and recovery, and document indexing. They do not certify every installed CLI workflow or a particular project's migration.

Optional live checks are in `checks/outside-judge.sh`, `checks/resume.sh`, and `checks/resume-codex.sh`. The sixteen cases in `evals/` exercise worker, explorer and judge behavior. Read their scripts before running them: live checks use provider accounts and create test files.

The protocol is an instruction layer. Use your CLI's permissions for hard execution limits. readback adds one: a hook denies Git commands that discard uncommitted work, such as `git reset --hard`, `git restore`, `git clean` and `git stash`. The Claude Code plugin enables it. Codex runs the same script after you add it to `~/.codex/hooks.json` and trust it; see [INSTALL](INSTALL.md#git-guard-for-codex). The deny check examines the command line; commit observation also reads repository content and review records. A hook that times out lets the command run. The publisher enforces its storage checks; it cannot establish that a test ran, an owner approved an action, or a handoff deserves PASS. Those claims still need evidence and review.

This is a personal workflow with cooperating local sessions. It has no multi-user access control, cloud synchronization, hosted collaboration, or task tracker integration. The index is rebuildable; authoritative records need your own backup policy. Retiring an existing Hub requires preserved history, verified recovery material, and fresh sessions using the new routing rules.

## Source map

| Path | Purpose |
|---|---|
| [PROTOCOL.md](PROTOCOL.md), [EXAMPLE.md](EXAMPLE.md) | Core rules and a worked task |
| [INSTALL.md](INSTALL.md) | Install, check, update, remove |
| [app/menu-bar/](app/menu-bar/) | Native macOS panel for project judge settings and session activity |
| [Shared projects](docs/shared-projects.md), [project identity](docs/project-identity.md) | Optional project memory and its operational contract |
| [Shared-project extension](docs/protocol-0.7-draft.md), [schemas/](schemas/) | Shipped extension, generic data shapes, and the explicitly draft handoff envelope |
| [output-styles/](output-styles/), [agents/](agents/), [skills/](skills/) | Claude Code instructions |
| [hooks/](hooks/), [portable/codex/](portable/codex/) | Hook that denies Git commands which discard work, and its Codex definition |
| [portable/skills/](portable/skills/), [scripts/sync.py](scripts/sync.py) | Generated portable instructions and bundled helpers |
| [scripts/project_registry.py](scripts/project_registry.py) | Project identity, validation, routing, and the legacy home of a linked worktree |
| [scripts/readback_settings.py](scripts/readback_settings.py) | Settings file of a project home: show, set and migrate-teammates |
| [scripts/readback_session.py](scripts/readback_session.py) | Session state: the mode override and the round authorizations |
| [scripts/judge_rounds.py](scripts/judge_rounds.py) | Round accounting of the outside judge |
| [hooks/judge_line.py](hooks/judge_line.py) | Hook that reads the owner's judge line |
| [hooks/guard_git.py](hooks/guard_git.py) | Git guard hook and its commit observation switch |
| [scripts/publish_records.py](scripts/publish_records.py) | Serialized Markdown publication and recovery |
| [scripts/project_index.py](scripts/project_index.py) | Document catalog, query, and freshness checks |
| [scripts/outside_judge.py](scripts/outside_judge.py) | Provider selection, restrictions, deadlines, and the receipt line |
| [scripts/validate_handoff.py](scripts/validate_handoff.py) | Structural validator for a contract and a handoff |
| [scripts/review_capture.py](scripts/review_capture.py) | Content capture of a Git worktree |
| [scripts/review_receipt.py](scripts/review_receipt.py) | Review records and the receipt line |
| [scripts/commit_observation.py](scripts/commit_observation.py) | Commit observation of the Git guard hook |
| [checks/](checks/), [evals/](evals/) | Offline checks and optional live fixtures |
| [LICENSE](LICENSE) | MIT |

The name comes from reading back an instruction before acting on it. The protocol borrows ideas from controlled language, EARS contracts, aviation readback, identified records, and independent verification. It claims no conformity or certification to those standards.
