#!/usr/bin/env python3
"""retention_audit.py – parse a YouTube Studio audience-retention CSV and find the leaks.

Ported from github.com/Jakeschincariol/youtube-agent-skill (MIT).
Extended: pipeline helper analyze_csv(), Telegram-ready summary, JSON export.

Three signals, three different fixes:
  HOOK LEAK  – what you lost in the first 30 seconds
  CLIFFS     – specific steep drops (a moment people left at)
  SLIDE      – steady bleed across the flat middle

Get the CSV from Studio → Analytics → a video → Engagement → Audience Retention chart → ↓ icon.

CLI:
    python tools/retention_audit.py retention.csv
    python tools/retention_audit.py retention.csv --transcript subtitles.srt --json

Python API:
    from tools.retention_audit import analyze_csv
    result = analyze_csv("retention.csv")
    print(result["hook_leak"], result["cliffs"])
"""
import csv, json, os, re, sys

# ── CSV loader ────────────────────────────────────────────────────────────────
def _load_csv(path: str) -> list[tuple[float, float]]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        for r in csv.reader(fh):
            nums = []
            for c in r:
                c = c.strip().replace("%", "").replace(",", "")
                try:    nums.append(float(c))
                except: nums.append(None)
            vals = [n for n in nums if n is not None]
            if len(vals) >= 2:
                rows.append((vals[0], vals[1]))
    return rows

# ── SRT / VTT loader (for cliff annotation) ───────────────────────────────────
def _parse_ts(s):
    s = s.strip().replace(",", ".")
    p = s.split(":")
    return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2]) if len(p) == 3 \
           else int(p[0]) * 60 + float(p[1])

def _load_srt(path: str) -> list[tuple[float, float, str]]:
    raw  = open(path, encoding="utf-8", errors="replace").read()
    cues, cur = [], None
    for line in raw.splitlines():
        m = re.match(r"\s*(\d[\d:.,]+)\s*-->\s*(\d[\d:.,]+)", line)
        if m:
            cur = [_parse_ts(m.group(1)), _parse_ts(m.group(2)), []]
            cues.append(cur)
        elif cur is not None and line.strip() and not line.strip().isdigit():
            cur[2].append(line.strip())
    return [(a, b, " ".join(t)) for a, b, t in cues if t]

# ── Core analysis ─────────────────────────────────────────────────────────────
def analyze_csv(path: str, transcript_path: str = None, duration_sec: float = None) -> dict:
    """Analyse a retention CSV. Returns:
    {points, start, end, hook_leak, hook_verdict, cliffs, slide_per_unit, said, summary}
    """
    rows = _load_csv(path)
    if len(rows) < 8:
        return {"error": "fewer than 8 data points in CSV"}

    xs, ys     = [r[0] for r in rows], [r[1] for r in rows]
    pct_axis   = max(xs) <= 100.5
    start      = ys[0] or 100.0

    # Hook window: first 30 s (or first 10 % if no duration given)
    if not pct_axis:
        cutoff = 30.0
    elif duration_sec:
        cutoff = 30.0 / duration_sec * 100
    else:
        cutoff = 10.0

    def at(x):
        return (x / 100.0 * duration_sec) if (pct_axis and duration_sec) else x

    hook_end  = min((y for x, y in rows if x <= cutoff), default=start)
    hook_leak = start - hook_end
    hook_verdict = "healthy" if hook_leak < 25 else "leaking" if hook_leak < 40 else "severe"

    # Cliffs
    drops = []
    for i in range(1, len(rows)):
        d    = ys[i - 1] - ys[i]
        span = xs[i] - xs[i - 1] or 1
        drops.append((d / span, xs[i - 1], xs[i], d))
    drops.sort(reverse=True)
    cliffs = [
        {"from": round(a, 2), "to": round(b, 2), "lost": round(d, 2),
         "at_seconds": round(at(a), 1) if (not pct_axis or duration_sec) else None}
        for _, a, b, d in drops[:5] if d > 0.8
    ]

    # Slide (average drop rate across the middle)
    mid   = [dd for dd, x0, _, _ in [(r[0], r[1], r[2], r[3]) for r in drops] if x0 > cutoff]
    slide = round(sum(mid) / len(mid), 3) if mid else 0.0

    # Annotate cliffs with spoken words if a transcript is provided
    said = {}
    if transcript_path and os.path.exists(transcript_path):
        cues = _load_srt(transcript_path)
        for c in cliffs:
            if c["at_seconds"] is None: continue
            t    = c["at_seconds"]
            near = [q[2] for q in cues if q[0] <= t + 4 and q[1] >= t - 4]
            said[str(c["from"])] = " ".join(near)[:140]

    # Human-readable summary
    lines = [
        f"HOOK LEAK   {hook_leak:.1f}% lost in the first 30 s [{hook_verdict}]",
        f"CLIFFS      {len(cliffs)} steep drop(s) detected",
    ]
    for c in cliffs:
        where = f"{c['at_seconds']:.0f}s" if c["at_seconds"] is not None else f"{c['from']}%"
        snippet = f'   "{said.get(str(c["from"]), "")}"' if said else ""
        lines.append(f"  -{c['lost']:5.1f}%  at {where:>8}{snippet}")
    lines.append(f"SLIDE       {slide:.3f}% per unit across the middle")
    if slide > 0.5:
        lines.append("  Pacing issue – cut the middle, do not rewrite it.")

    return {
        "points":        len(rows),
        "start":         start,
        "end":           ys[-1],
        "hook_leak":     round(hook_leak, 2),
        "hook_verdict":  hook_verdict,
        "cliffs":        cliffs,
        "slide_per_unit": slide,
        "said":          said,
        "summary":       "\n".join(lines),
    }

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    a   = sys.argv[1:]
    as_json = "--json" in a; a = [x for x in a if x != "--json"]
    tr  = a[a.index("--transcript") + 1] if "--transcript" in a else None
    dur = float(a[a.index("--duration") + 1]) if "--duration" in a else None
    files = [x for x in a if not x.startswith("--") and x not in (tr, str(dur) if dur else "")]
    if not files or not os.path.exists(files[0]):
        print(__doc__); sys.exit(1)
    result = analyze_csv(files[0], tr, dur)
    if as_json:
        print(json.dumps(result, indent=1))
    else:
        print(f"\n  {files[0]}   {result['points']} points   "
              f"{result['start']:.1f}% → {result['end']:.1f}%\n")
        print(result["summary"])
        print()

if __name__ == "__main__":
    main()
