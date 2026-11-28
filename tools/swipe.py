#!/usr/bin/env python3
"""swipe.py – rank collected videos by how far each beat its OWN channel median,
then classify the winning title/hook formula.

Ported from github.com/Jakeschincariol/youtube-agent-skill (MIT).
Extended: pipeline helper find_outliers(), uses bundled tools/hooks.json.

CLI:
    python tools/swipe.py collected.json
    python tools/swipe.py collected.json --min 2.0 --json

Python API:
    from tools.swipe import find_outliers, classify_title
    res = find_outliers(rows, min_multiple=1.5)
    print(res["outliers"], res["formula_counts"])
"""
import json, os, re, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
_HOOKS_JSON = os.path.join(HERE, "hooks.json")
if not os.path.exists(_HOOKS_JSON):
    _HOOKS_JSON = os.path.join(HERE, "..", "youtube-agent-skill", "skills", "yt-script", "hooks.json")

try:
    with open(_HOOKS_JSON, encoding="utf-8") as _fh:
        FORMULAS = json.load(_fh)["hooks"]
except FileNotFoundError:
    FORMULAS = []


def classify_title(title: str) -> str:
    """Classify a video title against the 21 hook formulas."""
    scored = []
    for f in FORMULAS:
        n = sum(1 for p in f["match"] if re.search(p, title or "", re.I))
        if n:
            scored.append((n, f["name"]))
    scored.sort(reverse=True)
    return scored[0][1] if scored else "Unclassified"


def find_outliers(rows: list[dict], min_multiple: float = 1.5, min_videos_per_channel: int = 4) -> dict:
    """Score each video as a multiple of its own channel's median views.
    Requires at least `min_videos_per_channel` videos per channel to avoid ranking on noise.
    Returns {total_videos, total_channels, outliers, skipped_thin_channels, formula_counts}.
    """
    if isinstance(rows, dict):
        rows = rows.get("videos", [])

    by_channel: dict[str, list[dict]] = {}
    for r in rows:
        by_channel.setdefault(r.get("channel", "?"), []).append(r)

    out, thin = [], []
    for ch, vids in by_channel.items():
        if len(vids) < min_videos_per_channel:
            thin.append((ch, len(vids)))
            continue
        views = [float(v.get("views", 0) or 0) for v in vids]
        med = statistics.median(views) if views else 0
        for v in vids:
            v_views = float(v.get("views", 0) or 0)
            m = (v_views / med) if med else 0.0
            if m >= min_multiple:
                title = v.get("title", "")
                out.append({
                    "channel": ch,
                    "title": title,
                    "views": int(v_views),
                    "median": int(med),
                    "multiple": round(m, 2),
                    "formula": classify_title(title),
                    "url": v.get("url", ""),
                })

    out.sort(key=lambda r: -r["multiple"])
    formula_counts: dict[str, int] = {}
    for r in out:
        formula_counts[r["formula"]] = formula_counts.get(r["formula"], 0) + 1

    return {
        "total_videos": len(rows),
        "total_channels": len(by_channel),
        "min_multiple": min_multiple,
        "outliers": out,
        "skipped_thin_channels": thin,
        "formula_counts": dict(sorted(formula_counts.items(), key=lambda x: -x[1])),
    }


def main():
    a = sys.argv[1:]
    as_json = "--json" in a
    a = [x for x in a if x != "--json"]
    lo = float(a[a.index("--min") + 1]) if "--min" in a else 1.5
    files = [x for x in a if not x.startswith("--") and not re.match(r"^[\d.]+$", x)]
    if not files or not os.path.exists(files[0]):
        print(__doc__)
        sys.exit(1)

    with open(files[0], encoding="utf-8") as fh:
        rows = json.load(fh)

    res = find_outliers(rows, min_multiple=lo)
    if as_json:
        print(json.dumps(res, indent=1))
        return

    print(f"\n  {res['total_videos']} videos across {res['total_channels']} channels, outliers at {lo}x or better\n")
    for r in res["outliers"][:25]:
        print(f"    {r['multiple']:5.2f}x  {r['views']:>9,}  vs {r['median']:>9,} median   "
              f"{r['channel'][:22]:<22} {r['title'][:52]}")
        print(f"            {r['formula']}")
    if not res["outliers"]:
        print("    nothing cleared the threshold - collect more per channel or lower --min")
    if res["skipped_thin_channels"]:
        thin = res["skipped_thin_channels"]
        print(f"\n  skipped {len(thin)} channel(s) with under 4 videos collected: "
              + ", ".join(f"{c} ({n})" for c, n in thin[:6]))
    if res["formula_counts"]:
        print("\n  formulas among the outliers")
        for f, n in res["formula_counts"].items():
            print(f"    {n:2d}x  {f}")
    print()


if __name__ == "__main__":
    main()
