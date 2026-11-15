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

## Other CLIs

No package exists for Codex or other CLIs.
