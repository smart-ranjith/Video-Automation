#!/usr/bin/env python3
"""comment_triage.py – triage YouTube comments into 4 piles and pick a pin candidate.

Ported from github.com/Jakeschincariol/youtube-agent-skill (skills/yt-comment).

Piles:
  1. QUESTIONS   – real questions (great next-video topics & pin candidates)
  2. CORRECTIONS – factual corrections / pushback worth acknowledging plainly
  3. PRAISE      – genuine appreciation (reply briefly to a few with specifics)
  4. BAIT        – trolls / spam / rage-bait (do NOT reply)

CLI:
    python tools/comment_triage.py comments.json [--json]

Python API:
    from tools.comment_triage import triage_comments, fetch_video_comments
    res = triage_comments(["How did you film this?", "Fake!", "Loved the pacing!"])
"""
import json, os, re, sys

BAIT_RE = re.compile(
    r"\b(fake|clickbait|trash|garbage|stupid|idiot|loser|scam|bot|sub4sub|check out my channel|unsubscribe)\b",
    re.I,
)
CORRECTION_RE = re.compile(
    r"\b(actually|wrong|incorrect|misleading|not true|false|mistake|correction|inaccurate|didn't happen)\b",
    re.I,
)
QUESTION_RE = re.compile(
    r"(\?|\b(how|why|what|where|when|which|who|did|does|is there|can you|could you)\b)",
    re.I,
)
PRAISE_RE = re.compile(
    r"\b(love|loved|great|awesome|underrated|best|amazing|brilliant|fire|subscribed|hooked|chills|wow)\b",
    re.I,
)


def classify_comment(text: str) -> str:
    """Classify a single comment into: QUESTIONS, CORRECTIONS, PRAISE, or BAIT."""
    t = (text or "").strip()
    if not t:
        return "BAIT"
    if BAIT_RE.search(t) and len(t.split()) <= 15:
        return "BAIT"
    if CORRECTION_RE.search(t):
        return "CORRECTIONS"
    if "?" in t or (QUESTION_RE.match(t) and len(t.split()) >= 4):
        return "QUESTIONS"
    if PRAISE_RE.search(t):
        return "PRAISE"
    return "PRAISE" if len(t.split()) >= 3 else "BAIT"


def triage_comments(comments: list) -> dict:
    """Triage a list of comment strings or dicts ({id, author, text, likes}).
    Returns {counts, piles, pin_candidate, topic_ideas}.
    """
    piles = {"QUESTIONS": [], "CORRECTIONS": [], "PRAISE": [], "BAIT": []}
    for item in comments:
        if isinstance(item, str):
            obj = {"text": item.strip(), "author": "", "likes": 0, "id": ""}
        else:
            obj = {
                "id": item.get("id", ""),
                "author": item.get("author", ""),
                "text": (item.get("text") or "").strip(),
                "likes": int(item.get("likes", 0) or 0),
            }
        if not obj["text"]:
            continue
        pile = classify_comment(obj["text"])
        piles[pile].append(obj)

    # Pin candidate: highest-liked / clearest question (never bait)
    pin_candidate = None
    if piles["QUESTIONS"]:
        pin_candidate = sorted(
            piles["QUESTIONS"],
            key=lambda c: (c["likes"], "?" in c["text"], len(c["text"])),
            reverse=True,
        )[0]

    topic_ideas = [c["text"][:120] for c in piles["QUESTIONS"][:5]]

    return {
        "counts": {k: len(v) for k, v in piles.items()},
        "piles": piles,
        "pin_candidate": pin_candidate,
        "topic_ideas": topic_ideas,
    }


def fetch_video_comments(credentials, video_id: str, max_results: int = 30) -> list[dict]:
    """Fetch top-level comments for a YouTube video using YouTube Data API v3."""
    from googleapiclient.discovery import build
    youtube = build("youtube", "v3", credentials=credentials)
    res = youtube.commentThreads().list(
        part="snippet",
        videoId=video_id,
        maxResults=min(100, max_results),
        order="relevance",
        textFormat="plainText",
    ).execute()
    out = []
    for item in res.get("items", []):
        top = item["snippet"]["topLevelComment"]
        sn = top["snippet"]
        out.append({
            "id": top["id"],
            "author": sn.get("authorDisplayName", ""),
            "text": sn.get("textDisplay", ""),
            "likes": int(sn.get("likeCount", 0) or 0),
        })
    return out


def main():
    a = sys.argv[1:]
    as_json = "--json" in a
    a = [x for x in a if x != "--json"]
    if not a or not os.path.exists(a[0]):
        print(__doc__)
        sys.exit(1)
    with open(a[0], encoding="utf-8") as fh:
        data = json.load(fh)
    res = triage_comments(data if isinstance(data, list) else data.get("comments", []))
    if as_json:
        print(json.dumps(res, indent=2))
        return
    print("\n  COMMENT TRIAGE")
    for k, n in res["counts"].items():
        print(f"    {k:<12} {n}")
    if res["pin_candidate"]:
        print(f"\n  PIN CANDIDATE: \"{res['pin_candidate']['text'][:100]}\"")
    if res["topic_ideas"]:
        print("\n  NEXT VIDEO TOPIC IDEAS (from questions):")
        for q in res["topic_ideas"]:
            print(f"    - {q}")
    print()


if __name__ == "__main__":
    main()
