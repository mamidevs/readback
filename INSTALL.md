# Install

Tested with Claude Code 2.1.325 on macOS, from a local copy of this repository.

## 1. Install the plugin

```
claude plugin marketplace add mamidevs/readback
claude plugin install readback@readback
```

## 2. Make the logbook

```
mkdir -p ~/logbook
```

readback writes records to `~/logbook/<project>/`.
`LOG.md` holds the log lines and the decisions. `records/` holds one file for each contract and each handoff.
`CURRENT.md` holds the goal, the open work and the next step.
`<project>` is the name of the repository's root folder.
Claude Code can ask before it reads or writes outside the project. Approve the access to the logbook.

## 3. Turn the style on

Installing the plugin does not turn the style on. You choose where it applies.

For one project, type this in a session:

```
/output-style readback:Readback
```

For every project, set this key in `~/.claude/settings.json`:

```json
{ "outputStyle": "readback:Readback" }
```

## 4. Check

| Step | Expected result |
|---|---|
| Type `/output-style` | The list shows `readback:Readback (current)`. |
| Ask for a small change | The answer ends in a brief of four fields. |
| Run `ls ~/logbook` | A folder with the name of your project. It holds `LOG.md`. |

The three agents are `readback:explorer`, `readback:worker` and `readback:judge`.

## Automatic outside judge

Install Python 3 and both provider CLIs. Log in to each CLI before the first judged task.
Claude Code leads use Codex with `gpt-6-astra` at high effort.
Codex leads use Claude Code with `fable` at high effort.
The integrations pass their host automatically. No `TEAMMATES.md` file is required.

The other provider receives the contract and handoff, and its CLI can read repository files.
The completed receipt stores the masked judgment. The lead keeps the decision and receipt reference in the handoff. Failed calls give `JUDGE none` and leave the work unaccepted.
There is no silent local or model fallback.
[TEAMMATES.example.md](TEAMMATES.example.md) describes optional overrides and legacy table compatibility.
Removing an override file restores the defaults; it does not disable the judge.

Offline routing checks and optional live checks:

```bash
python3 -m unittest discover -s checks -p 'test_outside_judge.py' -v
bash checks/outside-judge.sh codex
bash checks/outside-judge.sh claude
```

The last two commands use your provider accounts and consume model calls.

### Judge line for Claude Code

The plugin ships the hook `hooks/judge_line.py` for the events `UserPromptSubmit`, `SessionStart` and `SessionEnd`. The plugin's `hooks/hooks.json` names it for each event. You add nothing.
The hook reads the judge line that the owner writes as the first line of a prompt. [AMENDMENT-11.md](AMENDMENT-11.md) holds the forms.

### Dispatch guard

The plugin ships the hook `hooks/guard_dispatch.py` for PreToolUse with the matcher `Agent|Task`. It denies a `readback:worker` dispatch whose prompt names a CON id and delivers no statement of it. [AMENDMENT-12.md](AMENDMENT-12.md) holds the rules.
The hook runs in Claude Code only. A Codex lead has the two text rules only: the CONTRACT rule of the output style and section 3.2 of PROTOCOL.md.

## Try it without an install

```
git clone https://github.com/mamidevs/readback
claude --plugin-dir ./readback
```

The plugin loads for that session only.

## Review record maintenance

Completed judge details become eligible for deletion after 30 days. The judge helper checks on use, at most once a day. To preview what can be removed from an idle repository, run this from the Readback checkout. Use the host that resolves the project (`codex` or `claude`).

```sh
python3 -B scripts/review_retention.py --host codex /path/to/project
```

Add `--apply` to remove the listed details. Cleanup uses physical directory paths and verifies each record belongs to the repository. An older receipt from a removed worktree can survive if its ownership cannot be established. The round ledger continues to enforce the retry limit. Expired details may no longer be available through old receipt links.

Measurement output lives in the directory chosen for each run. Preview a completed batch with the command below. Add `--apply` to remove expired prompt, answer and error text while keeping the measurement results.

```sh
python3 -B checks/measure_judge.py --prune /path/to/completed-batch
```

These commands leave source contracts, handoffs and backups in their owner's custody. Older handoffs may contain a full copy of an answer.

## Update

```
claude plugin marketplace update readback
claude plugin update readback@readback
```

Restart Claude Code after an update.
The plugin has a version. An update arrives when the version changes.

## Remove

```
claude plugin uninstall readback@readback
claude plugin marketplace remove readback
```

`~/logbook` stays. Delete it yourself if you want to.

Run this from the intended repository; it deletes review records and observations shared by its main and linked worktrees.
```
rm -r "$(git rev-parse --git-common-dir)/readback"
```

The judge line and the round accounting keep local files in `~/logbook/.readback/sessions/` and `~/logbook/.readback/rounds/`. Delete both folders if you want to.

## OpenAI Codex

Codex has no output style and no plugin from this repository. It reads Agent Skills and `AGENTS.md`.

```
git clone https://github.com/mamidevs/readback
mkdir -p ~/.agents/skills ~/logbook
cp -R readback/portable/skills/* ~/.agents/skills/
printf '\nFor every task, and when I ask to continue earlier work, load the skill `readback` and follow it.\n' >> ~/.codex/AGENTS.md
```

The copy adds four folders: `readback`, `readback-worker`, `readback-explorer` and `readback-judge`.
The lead folder includes the outside-judge helper and its judge rules. Keep the complete folder when copying.
The line in `AGENTS.md` applies to every Codex session. It requires loading the lead rules for every task; plain questions need no BRIEF.
Codex can ask before it writes outside the project. Approve the access to the logbook.

Check: ask Codex for a small change. The answer ends in a brief of four fields, and `~/logbook/<project>/` holds `LOG.md` and `CURRENT.md`.

Update: pull the repository and copy the four folders again.
Remove: first remove the readback entry from `~/.codex/hooks.json` (see the Git guard below). Then delete the four folders from `~/.agents/skills/` and the line from `~/.codex/AGENTS.md`. The judge-line entries are readback entries too; the folders `~/logbook/.readback/sessions/` and `~/logbook/.readback/rounds/` are described under Remove above.

### Git guard for Codex

The lead folder holds `scripts/guard_git.py`, the hook that the Claude Code plugin runs.
Codex runs it before each shell command when `~/.codex/hooks.json` names it.
Copy the four folders first. Then, if you have no `~/.codex/hooks.json`, copy the file from this repository:

```
cp readback/portable/codex/hooks.json ~/.codex/hooks.json
```

If the file exists, add the `PreToolUse` entry of `portable/codex/hooks.json` to it.
Then start Codex, type `/hooks` and trust the readback hook. Codex skips a hook until you trust it.
The hook runs in every Codex session, also in sessions without the readback skill.
Commit observation needs commit_observation.py and review_capture.py beside the portable hook; the copied lead folder holds them.
Set READBACK_COMMIT_CHECK=off to turn the commit observation off.

Check: in a test repository with an uncommitted change, ask Codex to run `git reset --hard HEAD`.
Codex reports a refusal that starts with `readback blocks`, and the change stays.
A file in `~/.codex/hooks.json` is not proof. An untrusted hook does not run.

If the entry exists and the script does not, Codex refuses every shell command with `can't open file`.
Copy the folders again, or remove the entry.
An update that copies the folders again does not change the hook definition, so the trust stays.

Limits on Codex:
- Codex does not pass the folder of a command that sets its own workdir. The hook uses the session folder.
  A checkout of one file name can then pass when the session folder is outside the repository.
- Input sent to a shell that already runs is not checked again.
- A hook that times out lets the command run.

### Judge line for Codex

The lead folder holds `scripts/judge_line.py`. `portable/codex/hooks.json` names it for the events `UserPromptSubmit`, `SessionStart` and `SessionEnd`.
Merge the judge-line entries into $CODEX_HOME/hooks.json (default ~/.codex/hooks.json), trust them with /hooks, register each entry once and check one live prompt; without these steps a judge line changes nothing under Codex.
These steps describe documented Codex hook behaviour. It is not measured here.

## Other CLIs

No package exists for other CLIs. A CLI that reads Agent Skills can try `portable/skills/`. Nothing checks it.

## Optional shared project memory

Several code repositories can share one local project logbook without changing their working directories. See [shared projects](docs/shared-projects.md) and [project identity](docs/project-identity.md). Install the updated portable lead skill and its bundled scripts together. Existing unbound repositories retain the default behavior. A marked repository with a missing or invalid binding stops instead of creating a second logbook.

## macOS menu bar

On a Mac with macOS 14 or later and the Apple Command Line Tools, build and open the local app from this checkout:

```sh
app/menu-bar/build.sh && open app/menu-bar/build/Readback.app
```

Click the Readback icon, choose a project, then choose when its independent judge should run. Expand **Hakem modelleri** to choose the working host, model and effort. Claude Code work is reviewed by Codex. Codex work is reviewed by Claude. In **Her teslimde**, configure step and section-end reviews separately. **Bölüm sonunda** uses only the section-end route. Choose **Kaydet** to save the model and effort together. Claude Code and Codex read the saved project default. A session with its own mode override keeps that override. The app uses the macOS system Python and the settings reader bundled with the build. To build again alongside an existing copy, pass a new output path ending in `.app`.

Expand **Proje oturumları** and select a session to see its model and latest activities. The detail refreshes while open. **Oturum açık** means the Claude process exists or Codex holds its thread lock. **Kayıtlı** means that the provider still lists the session. Each activity follows the selected session’s local records. The panel shows operation names and timestamps. **Codex’te aç** opens the conversation in Codex. Use **Proje oturumları** to return to the list.
