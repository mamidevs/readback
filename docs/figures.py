#!/usr/bin/env python3
"""Writes the README figures. Run: python3 docs/figures.py

An image in a README cannot take the page colors, so each figure has a light
file and a dark file. Pass --preview to add a solid background to the dark
files in a separate folder, for a local look.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

THEMES = {
    "light": dict(fg="#1F2328", muted="#59636E", line="#C8D1DA", accent="#C4320A", bg="#FFFFFF"),
    "dark": dict(fg="#E6EDF3", muted="#9198A1", line="#3D444D", accent="#FF7A45", bg="#0D1117"),
}


def text(x, y, s, c, size=12, family=SANS, weight=400, anchor="middle", spacing=0):
    ls = f' letter-spacing="{spacing}"' if spacing else ""
    return (f'<text x="{x}" y="{y}" fill="{c}" font-family="{family}" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}"{ls}>{s}</text>')


def markers(t):
    out = []
    for name in ("fg", "accent", "muted"):
        out.append(f'<marker id="a-{name}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="8" markerHeight="8" '
                   f'orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" fill="{t[name]}"/></marker>')
    return "<defs>" + "".join(out) + "</defs>"


def wrap(w, h, label, body, t, bg):
    if bg:  # Quick Look crops to a square, so the preview canvas is square
        back, h = f'<rect width="{w}" height="{w}" fill="{t["bg"]}"/>', w
    else:
        back = ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'role="img" aria-label="{label}">{back}{markers(t)}{body}</svg>\n')


def flow(t, bg=False):
    w, h = 960, 560
    lanes = [(100, "OWNER", "you"), (290, "LEAD", "the main session"), (480, "WORKER", "another agent"),
             (670, "JUDGE", "a different agent"), (860, "LOGBOOK", "~/logbook")]
    b = [f'<rect x="22" y="86" width="268" height="446" fill="{t["accent"]}" opacity="0.07"/>']
    for x, name, sub in lanes:
        b.append(f'<rect x="{x - 72}" y="20" width="144" height="48" fill="none" stroke="{t["fg"]}" stroke-width="1.2"/>')
        b.append(text(x, 41, name, t["fg"], 13, MONO, 700, spacing=1.5))
        b.append(text(x, 58, sub, t["muted"], 11))
        b.append(f'<line x1="{x}" y1="68" x2="{x}" y2="532" stroke="{t["line"]}" stroke-width="1.2" stroke-dasharray="3 5"/>')
    msgs = [
        (290, 100, "DECISION", "one question, one recommendation", "accent", None),
        (100, 290, "GO", "for this decision only", "accent", None),
        (290, 480, "CONTRACT", "what done means", "fg", None),
        (480, 290, "READBACK", "task, limits, done when", "fg", None),
        (480, 290, "HANDOFF", "state, changes, checks", "fg", None),
        (290, 670, "handoff + contract", "sent for judgment", "fg", 385),
        (670, 290, "PASS / FAIL", "the first word", "fg", 575),
        (290, 860, "LOG", "and the stored records", "fg", 385),
        (290, 100, "BRIEF", "four fields, one next step", "accent", None),
    ]
    y = 112
    for x1, x2, name, note, color, lx in msgs:
        c = t[color]
        end = x2 - 3 if x2 > x1 else x2 + 3
        b.append(f'<line x1="{x1}" y1="{y}" x2="{end}" y2="{y}" stroke="{c}" stroke-width="1.6" marker-end="url(#a-{color})"/>')
        mid = lx if lx else (x1 + x2) / 2
        b.append(text(mid, y - 8, name, c, 13, MONO, 700, spacing=0.5))
        if note:
            b.append(text(mid, y + 15, note, t["muted"], 11))
        y += 46
    b.append(text(156, 548, "3 messages are for the owner", t["accent"], 11, SANS, 600))
    return wrap(w, h, "One task as messages between the owner, the lead, a worker, a judge and the logbook. "
                      "Three messages are addressed to the owner.", "".join(b), t, bg)


def places(t, bg=False):
    w, h = 960, 330

    def box(x, y, bw, bh, title, lines, dashed=False, c=None, top=28, first=52):
        c = c or t["fg"]
        dash = ' stroke-dasharray="4 4"' if dashed else ""
        out = [f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" fill="none" stroke="{c}" stroke-width="1.2"{dash}/>',
               text(x + bw / 2, y + top, title, c, 13, MONO, 700, spacing=1.5)]
        for i, s in enumerate(lines):
            out.append(text(x + bw / 2, y + first + i * 18, s, t["muted"], 12))
        return "".join(out)

    b = [f'<rect x="340" y="112" width="590" height="198" fill="none" stroke="{t["line"]}" stroke-width="1.2" stroke-dasharray="6 6"/>',
         text(356, 104, "YOUR MACHINE", t["muted"], 11, MONO, 700, anchor="start", spacing=1.5),
         box(30, 150, 200, 100, "REPOSITORY", ["the protocol", "1 output style, 3 agents"]),
         box(370, 150, 220, 100, "YOUR CLI", ["Claude Code or Codex"]),
         box(680, 150, 220, 100, "~/logbook", ["one folder for each project", "a log and the stored records"]),
         box(370, 12, 220, 62, "MODEL PROVIDERS", ["lead and outside judge"], dashed=True, c=t["muted"], top=25, first=45),
         f'<line x1="230" y1="200" x2="367" y2="200" stroke="{t["fg"]}" stroke-width="1.6" marker-end="url(#a-fg)"/>',
         text(282, 190, "install, update", t["fg"], 11),
         f'<line x1="590" y1="200" x2="677" y2="200" stroke="{t["fg"]}" stroke-width="1.6" marker-end="url(#a-fg)"/>',
         text(635, 190, "writes", t["fg"], 11),
         f'<line x1="480" y1="77" x2="480" y2="147" stroke="{t["muted"]}" stroke-width="1.2" stroke-dasharray="4 4" '
         f'marker-start="url(#a-muted)" marker-end="url(#a-muted)"/>',
         text(490, 100, "model calls", t["muted"], 11, anchor="start"),
         text(790, 286, "records stay local", t["accent"], 12, SANS, 600)]
    return wrap(w, h, "The repository installs rules into your CLI. The CLI writes records to a local "
                      "logbook folder. The lead and the outside judge use their model providers.", "".join(b), t, bg)


def main():
    preview = "--preview" in sys.argv
    out = HERE / "preview" if preview else HERE
    out.mkdir(exist_ok=True)
    for name, t in THEMES.items():
        (out / f"flow-{name}.svg").write_text(flow(t, bg=preview), encoding="utf-8")
        (out / f"places-{name}.svg").write_text(places(t, bg=preview), encoding="utf-8")
    print("wrote", ", ".join(sorted(p.name for p in out.glob("*.svg"))), "to", out.name)


if __name__ == "__main__":
    main()
