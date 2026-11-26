#!/usr/bin/env python3
"""titlelint.py – lint a YouTube title and thumbnail pairing before you publish.

Ported from github.com/Jakeschincariol/youtube-agent-skill (MIT).
Extended: pipeline helper lint_title(), JSON export, scoring threshold.

CLI:
    python tools/titlelint.py --title "..." --thumb "text on thumbnail"
    python tools/titlelint.py titles.txt          # one per line, ranked
    python tools/titlelint.py --title "..." --json

Python API:
    from tools.titlelint import lint_title
    result = lint_title("My Title Here", thumb="Thumbnail Text")
    if result["score"] < 60: print(result["issues"])
"""
import json, os, re, sys

DESKTOP, MOBILE, HARD = 60, 40, 100

VAGUE = {"amazing","incredible","insane","crazy","huge","massive","ultimate","best","powerful",
         "secret","revolutionary","mindblowing","epic","perfect","complete","everything"}
STOP  = {"the","a","an","of","for","to","in","on","and","or","is","are","with","your","you","my",
         "i","this","that","it","how","what","why"}

def _words(t): return re.findall(r"[a-z0-9']+", t.lower())

def lint_title(title: str, thumb: str = None) -> dict:
    """Lint a title (and optional thumbnail text).
    Returns: {title, chars, score (0-100), issues [(key, msg)], good [msg], pass (bool)}
    pass is True when score >= 60 (no blocking issues).
    """
    t = title.strip()
    n = len(t)
    issues, good = [], []

    # Length checks
    if n > HARD:
        issues.append(("length", f"{n} chars – YouTube hard limit is {HARD}"))
    elif n > DESKTOP:
        issues.append(("length", f"{n} chars – desktop search cuts near {DESKTOP}"))
    else:
        good.append(f"{n} chars, inside {DESKTOP}-char desktop cut")

    if n > MOBILE:
        head = t[:MOBILE].rsplit(" ", 1)[0]
        issues.append(("mobile", f'mobile shows "{head}…" – check subject survives'))

    # All-caps
    caps = [w for w in t.split() if len(w) > 2 and w.isupper()]
    if len(caps) > 2:
        issues.append(("shouting", f"{len(caps)} all-caps words – two is the ceiling"))
    elif caps:
        good.append(f"{len(caps)} all-caps word for emphasis")

    # Vague words
    v = [w for w in _words(t) if w in VAGUE]
    if v:
        issues.append(("vague", f"{', '.join(sorted(set(v)))} – swap for a number, name or date"))

    # Concrete figures
    nums = re.findall(r"\d[\d,.]*%?", t)
    if nums:
        good.append(f"carries a concrete figure ({', '.join(nums[:3])})")
    else:
        issues.append(("no-number", "no number, date or name – the most reliable single fix"))

    # Open question
    if t.endswith("?"):
        good.append("open question in the title")

    # Front-load
    front = [w for w in _words(t)[:3] if w not in STOP]
    if not front:
        issues.append(("front-load", "first three words are all filler – move subject forward"))

    # Thumbnail duplication
    if thumb:
        tw, hw = set(_words(t)) - STOP, set(_words(thumb)) - STOP
        shared = tw & hw
        if shared:
            issues.append(("duplicate", f"thumbnail repeats title on: {', '.join(sorted(shared))} – "
                                        "thumbnail should say what the title does not"))
        else:
            good.append("thumbnail and title carry different words")
        if len(_words(thumb)) > 4:
            issues.append(("thumb-length", f"{len(_words(thumb))} words on thumbnail – three is the ceiling at feed size"))

    score = max(0, min(100, 100 - 14 * len(issues) + 4 * len(good)))
    return {"title": t, "chars": n, "score": score, "issues": issues, "good": good, "pass": score >= 60}

# ── CLI ───────────────────────────────────────────────────────────────────────
def _show(r):
    print(f'\n  "{r["title"]}"')
    print(f"  {r['chars']} chars   score {r['score']}/100   {'✓ PASS' if r['pass'] else '✗ FAIL'}")
    for k, m in r["issues"]: print(f"    ✗  {k:<14} {m}")
    for m in r["good"]:      print(f"    ✓               {m}")

def main():
    a       = sys.argv[1:]
    as_json = "--json" in a; a = [x for x in a if x != "--json"]
    thumb   = a[a.index("--thumb") + 1] if "--thumb" in a else None
    if "--title" in a:
        rows = [lint_title(a[a.index("--title") + 1], thumb)]
    elif a and os.path.exists(a[0]):
        rows = [lint_title(l, thumb) for l in open(a[0]).read().splitlines() if l.strip()]
    else:
        print(__doc__); sys.exit(1)
    rows.sort(key=lambda r: -r["score"])
    if as_json: print(json.dumps(rows, indent=1)); return
    for r in rows: _show(r)
    print()

if __name__ == "__main__":
    main()
