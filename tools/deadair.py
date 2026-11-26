#!/usr/bin/env python3
"""deadair.py – build an edit-decision list from a timestamped transcript.

Ported from github.com/Jakeschincariol/youtube-agent-skill (MIT).
Extended: pipeline helper build_edl(), Whisper JSON support.

Three cut types: DEAD (gap > floor), FILLER (um/uh/etc cues), REPEAT (retake).

CLI:
    python tools/deadair.py transcript.srt
    python tools/deadair.py transcript.srt --floor 0.35 --json

Python API:
    from tools.deadair import build_edl
    edl = build_edl("subtitles.srt")
    print(edl["summary"])
"""
import json, os, re, sys

FILLER_ONLY = re.compile(
    r"^[\s,.-]*((um+|uh+|er+|ah+|so|okay|ok|right|yeah|like|anyway|basically|"
    r"actually|you know|i mean|let me see|hold on)[\s,.-]*)+$", re.I
)

def _parse_ts(s):
    s = s.strip().replace(",", ".")
    p = s.split(":")
    return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2]) if len(p) == 3 \
           else int(p[0]) * 60 + float(p[1])

def load_cues(path: str) -> list[tuple[float, float, str]]:
    """Load SRT / VTT / Whisper JSON → [(start, end, text), ...]."""
    raw = open(path, encoding="utf-8", errors="replace").read()
    if path.endswith(".json"):
        d    = json.loads(raw)
        segs = d.get("segments", d if isinstance(d, list) else [])
        return [(float(s["start"]), float(s["end"]), (s.get("text") or "").strip()) for s in segs]
    cues, cur = [], None
    for line in raw.splitlines():
        m = re.match(r"\s*(\d[\d:.,]+)\s*-->\s*(\d[\d:.,]+)", line)
        if m:
            cur = [_parse_ts(m.group(1)), _parse_ts(m.group(2)), []]
            cues.append(cur)
        elif cur is not None and line.strip() and not line.strip().isdigit():
            cur[2].append(line.strip())
    return [(a, b, " ".join(t)) for a, b, t in cues if t]

def _norm(t): return re.sub(r"[^a-z ]", "", t.lower()).split()

def build_edl(path: str, floor: float = 0.45) -> dict:
    """Build an edit-decision list from a transcript file.
    Returns {source, duration, cuts, removed_sec, out_sec, summary}.
    """
    cues = load_cues(path)
    if not cues:
        return {"error": "no cues found in transcript"}
    dur  = cues[-1][1]
    cuts = []

    for i, (s, e, t) in enumerate(cues):
        # FILLER cue
        if FILLER_ONLY.match(t):
            cuts.append({"kind": "FILLER", "start": s, "end": e, "why": t.strip()[:48]})
        # DEAD air gap
        if i:
            gap = s - cues[i - 1][1]
            if gap > floor:
                keep = floor / 2
                cuts.append({"kind": "DEAD",
                             "start": round(cues[i - 1][1] + keep, 3),
                             "end":   round(s - keep, 3),
                             "why":   f"{gap:.2f}s gap"})
        # REPEAT / retake
        if t.strip() and not FILLER_ONLY.match(t):
            j = i - 1
            while j >= 0 and (FILLER_ONLY.match(cues[j][2]) or not cues[j][2].strip()):
                j -= 1
            if j >= 0:
                a1, b1 = _norm(cues[j][2])[:5], _norm(t)[:5]
                if len(a1) >= 3 and a1 == b1:
                    cuts.append({"kind": "REPEAT", "start": cues[j][0], "end": cues[j][1],
                                 "why": f'restart of "{" ".join(a1)}"'})

    cuts    = sorted([c for c in cuts if c["end"] > c["start"]], key=lambda c: c["start"])
    removed = sum(c["end"] - c["start"] for c in cuts)
    out_sec = dur - removed

    summary = (f"{len(cuts)} cuts | {removed:.1f}s removable | "
               f"{out_sec:.1f}s final | {removed / dur * 100:.1f}% shorter")
    return {
        "source":      path,
        "duration":    round(dur, 3),
        "cuts":        cuts,
        "removed_sec": round(removed, 3),
        "out_sec":     round(out_sec, 3),
        "summary":     summary,
    }

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    a       = sys.argv[1:]
    as_json = "--json" in a; a = [x for x in a if x != "--json"]
    floor   = float(a[a.index("--floor") + 1]) if "--floor" in a else 0.45
    a       = [x for x in a if not x.startswith("--") and not re.match(r"^[\d.]+$", x)]
    if not a or not os.path.exists(a[0]):
        print(__doc__); sys.exit(1)
    edl = build_edl(a[0], floor)
    if "error" in edl:
        print(edl["error"]); sys.exit(1)
    if as_json:
        print(json.dumps(edl, indent=1)); return
    print(f"\n  {edl['source']}   {edl['duration']:.2f}s   {len(edl['cuts'])} cues\n")
    for c in edl["cuts"]:
        print(f"    {c['kind']:<7} {c['start']:8.2f} -> {c['end']:8.2f}   "
              f"{c['end']-c['start']:5.2f}s   {c['why']}")
    print(f"\n  {edl['summary']}\n")

if __name__ == "__main__":
    main()
