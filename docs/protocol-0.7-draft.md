# Shared-project extension 0.7

Status: the resolver, Markdown publisher, and document index ship in package v0.7.0.
[PROTOCOL.md](../PROTOCOL.md) holds the core v0.7 communication rules.
The extended handoff envelope described below remains a draft; this release does not implement its proposed acceptance semantics.
The filename is retained for existing links. This document does not install host instructions or prove a private migration succeeded.

## Scope

Optional project settings let several code repositories use one local Markdown logbook.
Each coding session stays in its own repository and resolves the shared home before reading or writing records.
An unconfigured repository keeps the original repository-name convention through explicit compatibility fallback.

The local implementation has three separate responsibilities:

| Component | Responsibility |
|---|---|
| [Project resolver](../scripts/project_registry.py) | Identity, activation, repository markers, and record destination |
| [Record publisher](../scripts/publish_records.py) | Serialized publication with conflict checks and an interruption journal |
| [Document index](../scripts/project_index.py) | Rebuildable discovery hints from explicitly selected documents |

Markdown files remain authoritative project records.
The design requires no SQLite database, embedding service, vector store, daemon, or server.
It supports a personal workflow with one record-writing lead; workers return handoffs.
Concurrent lead sessions must use the same publisher rather than independently overwriting shared files.
This coordination does not lock application code worktrees or prevent deliberate same-user bypass.

## Identity and activation

A project UUID identifies one project across registered repositories and worktrees.
A home UUID identifies its center; a repository UUID survives an explicit move or rebind.
A slug, directory name, remote URL, branch, or content hash does not create identity.

Both hosts use the same local registry and resolver.
Validation checks canonical paths, Git common directories, physical directory identities, project metadata, and exact profile bytes.
An unknown, conflicting, unsupported, or incomplete binding fails closed.
See [Project identity](project-identity.md) for commands and the complete routing contract.

`STAGED` allows explicit inspection only and returns `write_allowed: false`.
`ACTIVE` requires matching center metadata and `extensions[extension_namespace].storage` equal to `markdown-single-writer`.
Every live registered repository and worktree must contain a matching `.readback-project.json` marker.
The marker preserves logical project and repository identity when a checkout is copied or moved.
It does not register the new physical location; an unbound marked checkout cannot use legacy fallback.

Retired roots return `RETIRED` and cannot acquire a live record destination.
A preserved Hub repository may be explicitly rebound to a reserved recovery root without losing its UUID.
The active home and live code repositories remain separate from that historical checkout.

The public schemas describe generic structures:

| Schema | Scope |
|---|---|
| [Registry v1](../schemas/registry-v1.schema.json) | Project bindings and registry generation |
| [Project v1](../schemas/project-v1.schema.json) | Center identity and profile binding |
| [Repository marker v1](../schemas/repository-marker-v1.schema.json) | Stable logical IDs inside a code checkout |
| [Profile v1](../schemas/profile-v1.schema.json) | Core language, record types, and a private namespace |
| [Handoff v1 draft](../schemas/handoff-v1.schema.json) | Proposed extended handoff envelope |
| [Legacy reference v1](../schemas/legacy-reference-v1.schema.json) | Qualified historical reference shape |

Runtime validation uses the Python standard library.
Schema shape cannot prove filesystem identity, Git ownership, matching profile bytes, or historical evidence truth.

## Record publication

The lead resolves the project, reads current records, and prepares one publication request.
The request contains an operation UUID, expected registry generation, expected CURRENT hash, new records, LOG lines, and replacement CURRENT text.
Each new record declares `shared` scope or explicit registered repository UUIDs.
The lead assigns readable IDs; an existing record ID causes a conflict rather than an overwrite.
Branch, commit, check working directory, and actual results remain part of the handoff's evidence.

The publisher acquires the stable project lock before the registry lock and resolves the project again under those locks.
Generation and CURRENT hash checks reject a stale publication before preparing new work.
A durable journal preserves the request and previous CURRENT plus the previous LOG hash and append suffix before publication begins.
Records are created without overwriting existing files; LOG preserves its previous prefix; CURRENT is replaced last.
The publication records the created record hashes.

Separate Markdown writes are not a single atomic transaction.
A crash may leave a prepared journal and some published files.
A new operation then stops with `RECOVERY_REQUIRED`; retry the original operation and payload to finish it.
A retry accepts only the recorded before or intended after bytes; unexpected changes remain a conflict.
Completed identical operations are idempotent; reusing an operation UUID with different content is rejected.
The retained journal keeps the previous CURRENT available for inspection and recovery.

The publisher checks storage coordination and input structure.
It cannot prove that a claimed test ran, a commit exists, an owner approved an action, or an independent judge accepted the work.
Those remain lead, project-policy, and judge responsibilities.
Filesystem permissions do not revoke older open handles or stop the owner deliberately bypassing helpers.

## Language and evidence

Core field names and fixed status words remain English.
New core records use English; owner quotations and historical documents retain their original language.
Project-language descriptions may remain under the profile namespace in structured extensions.
Profile fields cannot rename or replace core fields.

`DONE`, `PARTIAL`, `BLOCKED`, and `UNABLE` retain their existing meanings.
Checks retain `VERIFIED`, `FAILED`, and `UNVERIFIED`.
A failed check remains `FAILED`; missing execution evidence remains `UNVERIFIED`.
`PASS` and `FAIL` are judge verdicts; only the owner grants `GO` or `NO-GO`.

The existing handoff keys remain `id`, `from`, `date`, `state`, `readback`, `summary`, `changes`, `commit`, `checks`, `contract`, `open`, `issues`, and `next`.
The extended draft schema proposes `schema_version`, `project_id`, `record_uid`, and `extensions`.
That proposed envelope is not a claim that the Markdown publisher enforces semantic acceptance or globally allocates record UUIDs.

`CURRENT` retains its fixed labels and 30-line limit.
Detailed navigation belongs in `PROJECT-INDEX.md` and source documents.
A summary or index never overrides a decision record or stronger current evidence.

## Selective reading

The document index contains relative paths, hashes, headings, and literal repository-name mentions.
Only explicitly selected document directories are scanned; hidden folders, outputs, symlinks, and excluded filenames are omitted.
Skipped entries remain visible in the index report; filename exclusions are not a complete secret detector.
The index is local and rebuildable, never an independent source of project truth.

Repository mentions rank discovery hints; they do not establish ownership or exclude other repositories' documents.
Shared and unclassified documents remain discoverable, as do documents mentioning only another repository.
Query results disclose total counts, returned counts, pagination, and scope categories.
A text query further narrows matching paths or document content.

Queries recheck current document inputs, so added, changed, and deleted files cannot silently preserve stale hints.
A stale saved catalog is reported, while query results come from the current scan without rewriting shared files.
The lead rebuilds the saved index explicitly.
This reduces the amount an agent must load into context; it does not promise fewer disk reads or measured latency gains.
No inferred ownership graph, semantic search, or automatic relevance guarantee is claimed.

## Historical preservation and migration

Qualified historical references retain this form:

```text
legacy:<source_uuid>:<snapshot_sha256>:<escaped-original-path>#<escaped-raw-id>
```

The relative POSIX path uses UTF-8 percent escapes with `/` preserved.
Raw identifiers use percent escapes without extra safe punctuation.
Empty paths, parent traversal, absolute paths, and noncanonical encodings are rejected.
An archive-source UUID is distinct from a live repository UUID.
A newer snapshot is never selected implicitly; ambiguous labels require qualification.
These helpers do not themselves build a complete historical reference map.

Migration must preserve original values, unknown fields, identifiers, evidence levels, Git history, and pending work.
Historical verdicts do not become new PASS decisions or owner approvals.
Original handoffs are not automatically converted into new DONE records.

A migration needs a current inventory, verified recovery material, an exact control-file change list, and a rollback rehearsal.
Retire previous hooks, synchronization entrypoints, and duplicate judging flows before activating the common home.
Preserve application runtime instructions independently from developer startup guidance.
Publish ACTIVE only after the prepared home, bindings, markers, and required local checks agree.
Record pre-existing link defects separately from migration regressions.

Passing resolver fixtures establishes routing behavior only.
Actual restore verification, installed-host startup, content preservation, and Fable review remain separate evidence for each private cutover.
The former SQLite and generated-view proposal is not a prerequisite for this shared Markdown design.
