#!/usr/bin/env python3
"""chapters.py – generate YouTube-compliant chapter timestamps from a transcript.

Ported from github.com/Jakeschincariol/youtube-agent-skill (MIT).
Extended: pipeline helper generate_chapters(), returns paste-ready block.

YouTube's rules (enforced here, not assumed):
  - First entry must be 00:00
  - At least 3 chapters
  - Each chapter >= 10 s

CLI:
    python tools/chapters.py transcript.srt
    python tools/chapters.py transcript.srt --target 8 --json

Python API:
    from tools.chapters import generate_chapters
    result = generate_chapters("subtitles.srt", target=7)
    print(result["paste_block"])   # paste straight into the description
"""
import json, os, re, sys
from tools.deadair import load_cues   # shared parser

STOP = set(
    "the a an of for to in on and or is are was were be been with this that it as at by from "
    "you your i my we our they them he she but so if then than there here what which who how "
    "when where why not no yes do does did just really very like about into over out up down "
    "can could will would should have has had get got make made go going went one two".split()
)

def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']{4,}", text.lower()) if w not in STOP}

def _mmss(t: float) -> str:
    t = int(t); h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def generate_chapters(path: str, target: int = 7) -> dict:
    """Generate chapters from a transcript file.
    Returns {valid, chapters, paste_block, error?}.
    """
    cues = load_cues(path)
    if len(cues) < 6:
        return {"valid": False, "error": "too few cues to chapter", "chapters": [], "paste_block": ""}

    dur   = cues[-1][1]
    cand  = []
    for i in range(1, len(cues)):
        gap    = cues[i][0] - cues[i - 1][1]
        before = " ".join(c[2] for c in cues[max(0, i - 12):i])
        after  = " ".join(c[2] for c in cues[i:i + 12])
        kb, ka = _keywords(before), _keywords(after)
        shift  = 1 - (len(kb & ka) / len(kb | ka)) if (kb | ka) else 0
        cand.append((gap * 1.6 + shift * 3.2, cues[i][0], i))

    cand.sort(reverse=True)
    picked, MIN = [0.0], 10.0
    for _, t, _ in cand:
        if len(picked) >= target: break
        if all(abs(t - p) >= MIN for p in picked) and dur - t >= MIN:
            picked.append(t)
    picked.sort()

    chapters = []
    for n, t in enumerate(picked):
        end  = picked[n + 1] if n + 1 < len(picked) else dur
        text = " ".join(c[2] for c in cues if c[0] >= t and c[1] <= end)
        kw   = sorted(_keywords(text), key=lambda w: -text.lower().count(w))
        draft_title = " ".join(w.capitalize() for w in kw[:3]) or "Section"
        chapters.append({
            "start":       round(t, 2),
            "label":       _mmss(t),
            "draft_title": draft_title,
            "seconds":     round(end - t, 2),
        })

    ok         = len(chapters) >= 3 and chapters[0]["start"] == 0 and all(c["seconds"] >= MIN for c in chapters)
    paste_block = "\n".join(f"{c['label']} {c['draft_title']}" for c in chapters)
    return {
        "valid":       ok,
        "chapters":    chapters,
        "paste_block": paste_block,
        "note":        "Retitle every line before pasting – these are topic words, not your words.",
    }

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    a       = sys.argv[1:]
    as_json = "--json" in a; a = [x for x in a if x != "--json"]
    target  = int(a[a.index("--target") + 1]) if "--target" in a else 7
    a       = [x for x in a if not x.startswith("--") and not x.isdigit()]
    if not a or not os.path.exists(a[0]):
        print(__doc__); sys.exit(1)
    result = generate_chapters(a[0], target)
    if as_json:
        print(json.dumps(result, indent=1)); return
    print()
    if "error" in result:
        print(f"  ERROR: {result['error']}"); return
    for c in result["chapters"]:
        print(f"  {c['label']} {c['draft_title']}")
    print(f"\n  {len(result['chapters'])} chapters  {'✓ VALID' if result['valid'] else '✗ INVALID – YouTube needs 3+, 00:00 first entry, 10 s minimum'}")
    print(f"  {result['note']}\n")

if __name__ == "__main__":
    main()
