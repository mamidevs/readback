# Project identity

An optional project binding routes several repositories to one private Markdown home.
Code work stays in each repository; both host entrypoints select the same record destination.
Unconfigured repositories retain the original repository-name logbook convention through explicit legacy fallback.
The helpers require Python 3, Git, and POSIX `fcntl` locking.
Validation runs on macOS; other operating systems remain unverified.

## Private files

The conventional registry location is `~/logbook/.readback/projects.json`.
Every command requires an explicit `--registry` path, including isolated rehearsals and synthetic tests.
Private paths, profile content, and project identities stay outside the public repository.

```text
<registry-parent>/
  projects.json
  projects.lock
  locks/<project_uuid>.lock
<project-home>/
  project.json
  profile.json
  CURRENT.md
  LOG.md
  records/
```

The registry holds `registry_version`, `generation`, and `projects`.
Each project binds its UUID, home UUID, slug, canonical home, versions, profile hash, repositories, and retired roots.
Each repository binds its UUID, canonical main root, Git common directory, and explicit additional worktrees.
Sharing a Git common directory does not automatically register a new worktree.

`home_identity`, `root_identity`, `git_common_dir_identity`, and each worktree's `identity` record device and inode numbers.
A directory replacement requires an explicit rebind retaining its logical UUID.
The registry does not support moving a registered home to another path.
These local observations are not portable cryptographic identities.

`project.json` and `profile.json` must agree with the registry.
The profile hash covers exact file bytes, including whitespace.
Copying a center's UUIDs does not register another location.
Unknown versions, duplicate JSON keys, unexpected core fields, and conflicting bindings fail closed.

Every live ACTIVE repository and registered worktree contains `.readback-project.json`:

```json
{"marker_version": 1, "project_uuid": "<project UUID>", "repo_uuid": "<repository UUID>"}
```

The marker holds logical IDs only; physical identity remains in the private registry.
Prepare matching markers before activation; a missing or mismatched live marker blocks resolution.
STAGED repositories may omit markers; any present marker must match.
Retired recovery roots may omit markers, preserving their original contents.
A copied, moved, or otherwise unbound marked repository cannot fall back to separate memory.
Rebind it explicitly; a missing registry does not bypass that requirement.

## Resolution and activation

The wrappers pin their host identity and share one implementation:

```text
python3 scripts/resolve_codex_project.py --registry <registry-path> --cwd <working-directory> --legacy-fallback
python3 scripts/resolve_claude_project.py --registry <registry-path> --cwd <working-directory> --legacy-fallback
```

The resolver reads configuration; it never creates records, changes working directory, or starts a model.
Its result identifies the project, repository, resolved home, profile, record paths, canonical working directory, worktree root, and Git common directory.
Record authors must additionally capture the branch, commit, check working directory, and actual test result for each claim.
Resolving a home carries no code-repository identity.

`STAGED` remains inactive.
`--inspect-staged` permits inspection and returns `write_allowed: false`.
Without that flag, ordinary resolution returns `INACTIVE`.

`ACTIVE` requires matching center and registry states, an exact profile hash, and this explicit extension value:

```json
{"extensions": {"example": {"storage": "markdown-single-writer"}}}
```

Here `example` must equal the profile's `extension_namespace`.
This fragment is part of the existing profile envelope, not a complete profile file.
An `ACTIVE` response returns `write_allowed: true`.
That flag means eligible routing under the profile; it grants neither filesystem permission nor a held writer lock.
Only the lead writes shared records; parallel workers return handoffs.
The writer must serialize publication using the project's persistent lock and reread current state before assigning IDs.
Separate Markdown files are not one atomic transaction; interrupted publication needs explicit reconciliation.
Project-specific acceptance and evidence policies remain declarative instructions.

`MIGRATING`, `LEGACY_RECOVERY`, and unknown states remain unsupported.
Registry helpers do not change directory modes or close older sessions.
Old sessions must refresh routing instructions before using the activated home.
An owner can bypass file permissions and helpers; this mechanism does not isolate hostile same-user writes.

## Fallback and retirement

Unknown locations are rejected unless `--legacy-fallback` is supplied.
A missing registry then permits an unrelated repository-name fallback without creating configuration directories.
An empty registry behaves the same way, with generation `0`; a missing registry reports generation `null`.
Malformed or conflicting registries always fail, even with fallback requested.
`LEGACY_FALLBACK` is a compatibility candidate, with `write_allowed: false`; it does not grant registered project authority.
The lead follows existing 0.6 record rules only for this explicit compatibility status.

An unregistered worktree of a known Git common directory is always rejected.
Fallback destinations overlapping registered homes or retired roots are rejected.
Copied center markers reject fallback even when the registry is missing.
The resolver never creates a fallback directory.
Symlink aliases to the same live location preserve its identity.

Retired roots return `RETIRED`, exit `2`, and `write_allowed: false`; the error names the new home.
The check covers both the requested path and its physical destination.
A compatibility symlink underneath a retired path cannot silently reactivate that entrypoint.
A former Hub may remain a registered repository at an explicitly rebound recovery location.
Preserve its repository UUID, Git history, and pending work; reserve both the former path and recovery root.
The recovery repository validates for preservation but remains unavailable as a live working location.

## Registry updates

`init` creates an empty registry at an explicit path.
`validate` checks a registry and its bound files.
`apply` takes a complete candidate and the expected current generation.
Project and repository UUIDs cannot be removed; retired-root reservations cannot be released.
An explicit repository rebind retains its existing UUID.

Updates acquire stable project locks in UUID order, then the stable registry lock.
They never wait for a project lock while holding the registry lock.
They reread and compare the generation and affected project set under those locks.
A stale candidate returns a conflict without replacing the registry.
Successful replacement flushes a temporary file, atomically replaces the registry, and flushes its directory.
Lock files remain stable; removing one to bypass contention is unsafe.

A complete center must validate before publication.
Registry replacement does not make profile edits, activation, or migration a transaction.
Prepare and verify those steps separately; intermediate mismatch blocks resolution.
Filesystem validation also verifies all bound repositories, including retained recovery roots.

## Profile and reference limits

The profile's `extension_namespace` names the only permitted key inside `extensions`; staged profiles may omit that object.
Runtime validation enforces namespace equality and the active storage requirement across files.
JSON Schema alone describes shape; it cannot enforce those cross-file bindings.
Changing a profile requires updating its exact hash explicitly.

Qualified legacy references bind source UUID, snapshot hash, original path, and original identifier.
Ambiguous aliases are rejected instead of selecting the newest or first match.
The resolver does not build the project's historical reference map.

[The shared-project extension](protocol-0.7-draft.md) describes the shipped behavior and the handoff envelope that remains a draft.
Routing tests establish local selection and identity behavior; they do not prove a private migration or installed-host workflow.
