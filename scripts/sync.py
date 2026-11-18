#!/usr/bin/env python3
"""Writes the portable skills from the Claude Code files. Run: python3 scripts/sync.py

The output style and the agent files are the source. The skills under
portable/skills/ are copies for a CLI that reads Agent Skills and has no
output style. Pass --check to fail when a copy is not current.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "portable" / "skills"

HOST = """## On this host

- This skill holds the lead rules. Follow it for every task, and when the owner asks to continue earlier work.
- Workers and explorers are sub-agents of this CLI. Tell them to use `readback-worker` and `readback-explorer` respectively.
- If this CLI cannot start a sub-agent, do the work yourself. The independent judge still uses the other provider.

"""

# (text in the output style, text in the portable skill)
LEAD_CHANGES = [
    ("--host claude --legacy-fallback", "--host codex --legacy-fallback"),
    ("Resolve the helper relative to this plugin's root.", "Resolve the helper relative to this skill's directory."),
    ("Use the `outside-judge` skill. This Claude Code lead selects a Codex judge automatically.\n",
     "This Codex lead selects Claude Fable at high effort automatically. No teammates table is required.\n"
     "Run the bundled helper relative to this skill's directory. Resolve its absolute path before running it.\n\n"
     "```\npython3 <this-skill-directory>/scripts/outside_judge.py --host codex <contract file> <handoff file> <repository folder>\n```\n"
     "The other provider reads the contract, handoff and repository files. Store the complete output.\n"
     "Fable has Read, Grep and Glob tools only. It cannot rerun shell checks.\n"),
    ("This plugin names the place: `~/logbook/<project>/`.", "The place for records is `~/logbook/<project>/`."),
]

ROLES = {
    "worker": "Worker rules of the Readback Protocol. Use it when the lead gives you one bounded task that changes files. You return one HANDOFF.",
    "explorer": "Explorer rules of the Readback Protocol. Use it when the lead gives you a question to answer with evidence. You change no file and return one HANDOFF.",
    "judge": "Judge rules of the Readback Protocol. Use it only when the owner or a check names it. A lead does not use it to accept work. You change no file. Your first word is PASS or FAIL.",
}


def body(path):
    """The text of a file below its frontmatter."""
    text = path.read_text(encoding="utf-8")
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).lstrip("\n")


def skill(name, description, text):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{text}"


def build():
    lead = body(ROOT / "output-styles" / "readback.md")
    for old, new in LEAD_CHANGES:
        if lead.count(old) != 1:
            raise SystemExit(f"sync.py: the output style no longer holds this text once: {old[:60]!r}")
        lead = lead.replace(old, new)
    first, rest = lead.split("\n\n", 1)
    files = {
        "readback/SKILL.md": skill(
            "readback",
            "Lead rules of the Readback Protocol: result first, four-field brief, GO and NO-GO decisions, contracts, "
            "records and resume. Use it for every task, and when the owner asks to continue earlier work.",
            first + "\n\n" + HOST + rest),
    }
    for role, description in ROLES.items():
        files[f"readback-{role}/SKILL.md"] = skill(f"readback-{role}", description, body(ROOT / "agents" / f"{role}.md"))
    files["readback/scripts/outside_judge.py"] = (ROOT / "scripts" / "outside_judge.py").read_text(encoding="utf-8")
    files["readback/scripts/judge.md"] = body(ROOT / "agents" / "judge.md")
    files["readback/scripts/guard_git.py"] = (ROOT / "hooks" / "guard_git.py").read_text(encoding="utf-8")
    for helper in ("project_registry.py", "resolve_codex_project.py", "resolve_claude_project.py", "project_index.py", "publish_records.py",
                   "review_receipt.py", "review_capture.py", "validate_handoff.py", "commit_observation.py",
                   "readback_settings.py", "readback_session.py"):
        files[f"readback/scripts/{helper}"] = (ROOT / "scripts" / helper).read_text(encoding="utf-8")
    return files


def main():
    files = build()
    if "--check" in sys.argv:
        stale = [n for n, text in files.items()
                 if not (OUT / n).exists() or (OUT / n).read_text(encoding="utf-8") != text]
        if stale:
            print("not current:", ", ".join(stale))
            raise SystemExit(1)
        print("portable skills are current")
        return
    for name, text in files.items():
        path = OUT / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print("wrote", ", ".join(sorted(files)), "to portable/skills")


if __name__ == "__main__":
    main()
