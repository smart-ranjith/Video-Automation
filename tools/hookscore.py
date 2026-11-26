#!/usr/bin/env python3
"""hookscore.py – score a YouTube hook before you waste a take on it.

Ported from github.com/Jakeschincariol/youtube-agent-skill (MIT).
Extended: batch scoring, JSON export, pipeline helper score_hook().

Five properties, 0-100 each.
Verdict = 60 % mean + 40 % weakest (weakest-link weighting is intentional).

CLI:
    python tools/hookscore.py --hook "one line"
    python tools/hookscore.py hooks.txt          # one hook per line, ranked
    python tools/hookscore.py --json hooks.txt

Python API:
    from tools.hookscore import score_hook, score_hooks
    best = score_hooks(["hook 1", "hook 2"])[0]  # sorted best first
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
_HOOKS_JSON = os.path.join(HERE, "..", "youtube-agent-skill", "skills", "yt-script", "hooks.json")

try:
    FORMULAS = json.load(open(_HOOKS_JSON))["hooks"]
except FileNotFoundError:
    FORMULAS = []

# ── Lexicons ──────────────────────────────────────────────────────────────────
FILLER = {"basically","actually","literally","just","really","very","so","kind","sort","like",
          "guys","hey","welcome","today","video","subscribe","channel"}
VAGUE  = {"amazing","incredible","insane","crazy","huge","massive","game","changer","secret",
          "powerful","ultimate","best","revolutionary","mind","blowing","unbelievable"}
CONCRETE  = re.compile(r"\b(\d[\d,.]*\s?(%|k|m|x|s|m|h)?|\$\d|\d+\s?(second|minute|hour|day|week|month|year)s?)\b", re.I)
YOU       = re.compile(r"\b(you|your|you're|youre|yourself)\b", re.I)
STAKE     = re.compile(r"\b(lose|lost|wasting|waste|quit|fail|broke|cost|risk|before|stop|never|die|dying|dead)\b", re.I)
CURIOSITY = re.compile(r"\b(why|how|what|which|until|before|but|nobody|almost|except|reason|actually)\b", re.I)

def _words(t): return re.findall(r"[a-z0-9'%$.]+", t.lower())

def _specificity(t):
    w = _words(t)
    if not w: return 0
    nums   = len(CONCRETE.findall(t))
    vague  = sum(1 for x in w if x in VAGUE)
    filler = sum(1 for x in w if x in FILLER)
    s = 34 + nums * 22 - vague * 16 - filler * 5
    s += min(18, 6 * sum(1 for x in t.split()[1:] if x[:1].isupper()))
    return max(0, min(100, s))

def _address(t):
    n     = len(YOU.findall(t))
    first = 30 if YOU.search(" ".join(t.split()[:6])) else 0
    return max(0, min(100, 26 + n * 20 + first))

def _stakes(t):
    n = len(STAKE.findall(t))
    return max(0, min(100, 22 + n * 26 + (14 if CONCRETE.search(t) else 0)))

def _curiosity(t):
    n      = len(CURIOSITY.findall(t))
    q      = 18 if t.strip().endswith("?") else 0
    closed = -18 if re.search(r"\b(because|so that|which means)\b", t, re.I) else 0
    return max(0, min(100, 24 + n * 17 + q + closed))

def _brevity(t):
    n = len(_words(t))
    if n == 0: return 0
    if 9 <= n <= 24: return 100
    if n < 9:  return max(30, 100 - (9 - n) * 11)
    return max(10, 100 - (n - 24) * 7)

PROPS = [("SPECIFICITY", _specificity), ("ADDRESS", _address),
         ("STAKES", _stakes), ("CURIOSITY", _curiosity), ("BREVITY", _brevity)]

FIX = {
    "SPECIFICITY": "swap one adjective for a number, a name or a date",
    "ADDRESS":     "say 'you' in the first six words",
    "STAKES":      "name what it costs them to keep doing it the current way",
    "CURIOSITY":   "cut the half of the sentence that answers itself",
    "BREVITY":     "9 to 24 words. Read it out loud and stop where you run out of breath",
}

def _classify(t):
    best, hits = None, 0
    for f in FORMULAS:
        n = sum(1 for p in f["match"] if re.search(p, t, re.I))
        if n > hits: best, hits = f, n
    return (best["name"] if best else "Unclassified"), hits

def band(v): return "STRONG" if v >= 72 else "WORKABLE" if v >= 55 else "WEAK"

# ── Public API ────────────────────────────────────────────────────────────────
def score_hook(text: str) -> dict:
    """Score a single hook string. Returns a dict with keys:
    hook, properties, verdict (0-100), band, formula, fix_tip, matched.
    """
    parts   = {n: fn(text) for n, fn in PROPS}
    vals    = list(parts.values())
    verdict = round(0.6 * (sum(vals) / len(vals)) + 0.4 * min(vals))
    name, hits = _classify(text)
    low     = min(parts, key=parts.get)
    return {
        "hook":       text.strip(),
        "properties": parts,
        "verdict":    verdict,
        "band":       band(verdict),
        "formula":    name,
        "matched":    hits,
        "fix_tip":    FIX[low],
        "weakest":    low,
    }

def score_hooks(hooks: list[str]) -> list[dict]:
    """Score a list of hooks and return them sorted best-first."""
    return sorted([score_hook(h) for h in hooks if h.strip()], key=lambda r: -r["verdict"])

def best_hook(hooks: list[str]) -> dict:
    """Return the single best-scoring hook dict from a list."""
    scored = score_hooks(hooks)
    return scored[0] if scored else {}

# ── CLI ───────────────────────────────────────────────────────────────────────
def _report(r):
    print(f"\n  {r['hook']}")
    print(f"  {'-' * min(72, max(20, len(r['hook'])))}")
    for k, v in r["properties"].items():
        print(f"    {k:<12} {v:3d}  {'#' * (v // 5)}")
    print(f"    {'VERDICT':<12} {r['verdict']:3d}  {r['band']}")
    print(f"    formula      {r['formula']}")
    print(f"    weakest fix  {r['fix_tip']}")

def main():
    a       = sys.argv[1:]
    as_json = "--json" in a; a = [x for x in a if x != "--json"]
    if "--hook" in a:
        lines = [a[a.index("--hook") + 1]]
    elif a and os.path.exists(a[0]):
        lines = [l for l in open(a[0]).read().splitlines() if l.strip()]
    else:
        print(__doc__); sys.exit(0)
    out = score_hooks(lines)
    if as_json:
        print(json.dumps([{k: v for k, v in r.items() if k != "matched"} for r in out], indent=1))
        return
    for r in out: _report(r)
    if len(out) > 1:
        print(f"\n  winner: {out[0]['hook']}  ({out[0]['verdict']}, {out[0]['band']})\n")

if __name__ == "__main__":
    main()
