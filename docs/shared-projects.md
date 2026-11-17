# Shared project logbooks

A project may contain several Git repositories. Each repository keeps its working directory and code history; all bound repositories use the same local logbook home. Without a project binding, existing repository-name behavior remains available.

The private registry is `~/logbook/.readback/projects.json`. Each live repository has a `.readback-project.json` marker containing `marker_version`, `project_uuid` and `repo_uuid`. A moved or copied marked repository requires an explicit rebind. It never silently starts another logbook. See [project identity](project-identity.md) for the registry and profile schema.

At startup the lead resolves the actual working directory, shows repository/project/home, and reads the shared profile and CURRENT. Resolution does not change directories. Record checks retain the repository UUID, worktree, branch, commit and command working directory. A test in one repository proves nothing about another repository.

## Markdown publication

An ACTIVE profile declares `storage: markdown-single-writer` in its named extension. Only the lead publishes records; workers return their handoffs. `scripts/publish_records.py` acquires the same project and registry locks as the resolver configuration writer. Its request includes a unique operation UUID, expected registry generation, expected CURRENT SHA-256 (null when absent), records, LOG lines and the next CURRENT text.

Every new record declares `scope: shared` or a list of registered repository UUIDs. Record IDs are exclusive. The publication journal retains the previous CURRENT and request. Publication order is journal, immutable record files, LOG content, CURRENT, then committed receipt. LOG includes record content hashes. CURRENT remains at most30 lines.

This is not a transaction across files. An interrupted publication remains visible in `.readback/publications/` and blocks another publication. Retry the exact saved request with the same operation UUID to complete it. Changed files or generations return a conflict and require reconciliation; never delete an unfinished journal or silently discard an orphan record. Reusing an operation UUID with different content fails.

The publisher checks CURRENT under the lock, so a stale lead cannot overwrite another lead's summary. It limits requests to1MiB, journals to4MiB and LOG to16MiB; a limit failure preserves existing data. Archive/rotation is deliberate maintenance, not automatic deletion. Locks coordinate cooperating tools; they do not prevent an unrestricted same-user process from editing files directly. The publication lock does not cover model calls, tests, or code editing.

## Selective reading

`project_index.py build` creates `.readback/project-index.json` and `PROJECT-INDEX.md` from explicitly selected Markdown/text directories. Hidden controls, output trees, symlinks, hardlinks, unsupported files and oversized files are excluded and reported. The index stores paths, titles, headings/lines, content hashes and literal repository mentions. It does not scan arbitrary application source code or contact a provider.

`query --repo <name> --text <term>` ranks matching repository mentions first. It never hides documents merely because they mention a different repository. Counts show requested-repo, other-repo and unclassified results; pagination is explicit. Each query computes fresh hints without writing and reports whether the persistent cache is stale. The lead can rebuild the cache.

The index reduces the material an agent needs to read; it does not promise fewer filesystem reads or a measured latency gain. Text queries search the selected document bodies and paths and return the matching line. A source document, code and current evidence always outrank the index. Historical claims require current verification.

## Migration boundaries

Preserve a current backup and verified restore before retiring an existing Hub. Keep its Git history, pending changes, raw documents and metadata in private recovery storage. Keep numbered document directories unchanged. Archive old workflow controls instead of loading them in the new home.

A retired path is a real read-only directory with explicit compatibility links for historical documents and failing legacy workflow entrypoints. It is not a whole-root symlink to the active home. Reserve both the retired path and archived Git root in the registry. ACTIVE is published last. Existing sessions must reload the updated instructions; no tool can revoke instructions already loaded into an unrestricted process.

Rollback preserves the active home and every post-cutover record before restoring old entrypoints. A rollback is not a code, database or deployment rollback. Only exact expected hashes may be replaced automatically; unexpected edits require reconciliation.
