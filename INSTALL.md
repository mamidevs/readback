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

## A judge at another provider (optional)

This step needs the `codex` command and its login.

```
printf 'judge | codex | gpt-6-astra | high\n' > ~/logbook/TEAMMATES.md
```

Edit the model and the effort in the row.
The other provider then sees the contract, the handoff and the repository folder of each judged task.
Remove the file to turn it off.

## Try it without an install

```
git clone https://github.com/mamidevs/readback
claude --plugin-dir ./readback
```

The plugin loads for that session only.

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

## OpenAI Codex

Codex has no output style and no plugin from this repository. It reads Agent Skills and `AGENTS.md`.

```
git clone https://github.com/mamidevs/readback
mkdir -p ~/.agents/skills ~/logbook
cp -R readback/portable/skills/* ~/.agents/skills/
printf '\nFor every task, and when I ask to continue earlier work, load the skill `readback` and follow it.\n' >> ~/.codex/AGENTS.md
```

The copy adds four folders: `readback`, `readback-worker`, `readback-explorer` and `readback-judge`.
The line in `AGENTS.md` applies to every Codex session. It loads the lead rules for a task, not for a plain question.
Codex can ask before it writes outside the project. Approve the access to the logbook.

Check: ask Codex for a small change. The answer ends in a brief of four fields, and `~/logbook/<project>/` holds `LOG.md` and `CURRENT.md`.

Update: pull the repository and copy the four folders again.
Remove: delete the four folders from `~/.agents/skills/` and the line from `~/.codex/AGENTS.md`.

## Other CLIs

No package exists for other CLIs. A CLI that reads Agent Skills can try `portable/skills/`. Nothing checks it.
