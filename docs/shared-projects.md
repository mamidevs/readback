# Shared project logbooks

A project may contain several Git repositories. Each repository keeps its working directory and code history; all bound repositories use the same local logbook home. Without a project binding, existing repository-name behavior remains available.

The private registry is `~/logbook/.readback/projects.json`. Each live repository has a `.readback-project.json` marker containing `marker_version`, `project_uuid` and `repo_uuid`. A moved or copied marked repository requires an explicit rebind. It never silently starts another logbook. See [project identity](project-identity.md) for the registry and profile schema.

At startup the lead resolves the actual working directory, shows repository/project/home, and reads the shared profile and CURRENT. Resolution does not change directories. Record checks retain the repository UUID, worktree, branch, commit and command working directory. A test in one repository proves nothing about another repository.

## Markdown publication

Both hosts publish through `scripts/publish_records.py`. An ACTIVE profile declares `storage: markdown-single-writer` in its named extension. For an unbound repository pass `--legacy-fallback` to use its resolved legacy home. A staged or invalid binding still stops publication.

Start by taking a snapshot of your task. Use the same task ID and working directory for the publication. In a Readback checkout, for example:

```sh
python3 -B scripts/publish_records.py --registry "$HOME/logbook/.readback/projects.json" --cwd "$PWD" --host codex --legacy-fallback --task import-catalog
```

The snapshot contains the repository and worktree identity, registry generation, task block digest and current OPEN/NEXT values. A missing block has a null digest. Keep the generation and digest from the snapshot you actually edited. To publish, add `--request /path/to/request.json` to the same command. A task request contains:

```json
{
  "operation_id": "f38a4c5d-cfce-4cdb-9ac5-c56c588071ae",
  "expected_generation": null,
  "expected_block_sha256": null,
  "records": [],
  "log_lines": ["LOG-20261119-01 | catalog | doc:import | Started checking input rows. | Import request. | - | input.csv"],
  "open": "Check the remaining input rows.",
  "next": "Validate the supplier column."
}
```

Use a fresh operation UUID and available record IDs. Copy the actual snapshot values in place of the sample nulls. Each immutable record declares shared scope or registered repository UUIDs. Tasks share GOAL and IN FORCE. Their OPEN and NEXT entries live in separate blocks keyed by repository, task ID and worktree. Older unscoped text remains readable. Removing a branch or working tree leaves its unresolved work in the logbook.

The publisher reads the latest CURRENT under the publication lock, checks the target block digest, and merges the update. Distinct tasks can publish from the same starting document. A stale update to the same block returns CONFLICT before recording the losing request. Reread that block and reconcile its work. Duplicate LOG or record IDs also return a conflict. Allocate a free ID before submitting the revised request. For an interrupted operation, retry its exact saved request with its original UUID.

Use the whole-document request for project-wide changes. It uses `current` and `expected_current_sha256` in place of the task fields. Base it on the full document and preserve all task blocks. The publication journal precedes immutable records, LOG and CURRENT. The committed receipt follows those writes. Recovery checks the original state and content hashes. Locks coordinate cooperating writers and are held during publication only.

## Selective reading

`project_index.py build` creates `.readback/project-index.json` and `PROJECT-INDEX.md` from explicitly selected Markdown/text directories. Hidden controls, output trees, symlinks, hardlinks, unsupported files and oversized files are excluded and reported. The index stores paths, titles, headings/lines, content hashes and literal repository mentions. It does not scan arbitrary application source code or contact a provider.

`query --repo <name> --text <term>` ranks matching repository mentions first. It never hides documents merely because they mention a different repository. Counts show requested-repo, other-repo and unclassified results; pagination is explicit. Each query computes fresh hints without writing and reports whether the persistent cache is stale. The lead can rebuild the cache.

The index reduces the material an agent needs to read; it does not promise fewer filesystem reads or a measured latency gain. Text queries search the selected document bodies and paths and return the matching line. A source document, code and current evidence always outrank the index. Historical claims require current verification.

## Migration boundaries

Preserve a current backup and verified restore before retiring an existing Hub. Keep its Git history, pending changes, raw documents and metadata in private recovery storage. Keep numbered document directories unchanged. Archive old workflow controls instead of loading them in the new home.

A retired path is a real read-only directory with explicit compatibility links for historical documents and failing legacy workflow entrypoints. It is not a whole-root symlink to the active home. Reserve both the retired path and archived Git root in the registry. ACTIVE is published last. Existing sessions must reload the updated instructions; no tool can revoke instructions already loaded into an unrestricted process.

Rollback preserves the active home and every post-cutover record before restoring old entrypoints. A rollback is not a code, database or deployment rollback. Only exact expected hashes may be replaced automatically; unexpected edits require reconciliation.
