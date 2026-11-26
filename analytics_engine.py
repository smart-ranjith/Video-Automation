"""
Weekly YouTube Dashboard Analyzer & Dynamic Trigger Engine.

Queries YouTube Analytics API v2 and YouTube Data API v3 once per week (or on demand),
evaluates high-seen videos, watch time, retention drop-off curves (top winners vs weak videos),
traffic sources, subscriber conversions, topic saturation, and peak weekdays.

Outputs:
  - channel_strategy.json : Active strategy, retention pacing targets, anti-repetition bans,
                            winning archetypes, and dynamic scheduling/trigger rules.
  - performance_report.json : Human-readable weekly dashboard summary with live metrics.
"""

import argparse
import base64
import datetime
import json
import os
import pickle
import re
import statistics
from collections import Counter

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

STRATEGY_FILE = "channel_strategy.json"
PERFORMANCE_REPORT_FILE = "performance_report.json"
TOPIC_HISTORY_FILE = "topic_history.json"
VIEWER_REQUESTS_FILE = "viewer_requests.json"
CLIFFHANGER_FILE = "cliffhanger.json"

YT_ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
YT_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YT_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
YT_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

WEEKLY_INTERVAL_DAYS = 7

# High-retention / mass-appeal Shorts archetypes
CONTENT_ARCHETYPES = [
    {
        "id": "dark_psychology",
        "label": "Dark Psychology & Mind Perception Glitches",
        "tavily_query": "viral psychology tricks optical illusions human brain memory hacks trending",
        "prompt_directive": (
            "Focus on mind-bending psychology tricks, subconscious persuasion, or sensory/visual "
            "brain glitches where viewers feel compelled to pause or replay to test themselves."
        ),
    },
    {
        "id": "true_survival",
        "label": "High-Stakes True Survival & Impossible Escapes",
        "tavily_query": "unbelievable true survival stories impossible rescues real life escapes",
        "prompt_directive": (
            "Focus on intense real-life survival against impossible odds, ticking-clock rescues, "
            "or brilliant true heists told moment-by-moment with escalating stakes."
        ),
    },
    {
        "id": "chilling_mysteries",
        "label": "Chilling Unsolved Mysteries & Eerie True Stories",
        "tavily_query": "creepy unsolved true mysteries bizarre vanishings ghost ships historical oddities",
        "prompt_directive": (
            "Focus on haunting real-life vanishings, eerie historical anomalies, or chilling "
            "unexplained recordings—avoid generic ocean brine/vortex tropes."
        ),
    },
    {
        "id": "forbidden_secrets",
        "label": "Forbidden Places, Locked Vaults & Hidden History",
        "tavily_query": "most forbidden places hidden underground cities secret vaults historical mysteries",
        "prompt_directive": (
            "Focus on strictly sealed vaults, lost underground cities, classified cold-war relics, "
            "or bizarre archaeological discoveries—pick fresh locations never covered on the channel."
        ),
    },
]

STOP_TOKENS = {
    "the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "is", "are", "was", "were",
    "with", "your", "you", "my", "i", "this", "that", "it", "how", "what", "why", "who", "when",
    "where", "from", "into", "over", "under", "about", "shorts", "short", "explained", "mystery",
    "mysteries", "secret", "secrets", "hidden", "found", "minute", "minutes", "min", "meter",
    "meters", "hour", "hours", "second", "seconds", "day", "days", "years", "never", "still",
    "nobody", "everyone", "scientists", "science", "can", "could", "would", "did", "using",
    "out", "off", "all", "not", "now", "one", "two", "three", "four", "new", "man", "men",
    "see", "way", "after", "before", "that", "than", "then", "they", "them", "their", "has",
    "had", "have", "its", "made", "make", "only", "real", "true", "super", "cold",
}


def restore_google_secrets():
    """Restore client_secrets.json and token.pickle from CI base64 env vars if missing."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    if not os.path.exists("client_secrets.json") and os.environ.get("CLIENT_SECRETS_BASE64"):
        with open("client_secrets.json", "wb") as f:
            f.write(base64.b64decode(os.environ["CLIENT_SECRETS_BASE64"]))
    if not os.path.exists("token.pickle") and os.environ.get("TOKEN_PICKLE_BASE64"):
        with open("token.pickle", "wb") as f:
            f.write(base64.b64decode(os.environ["TOKEN_PICKLE_BASE64"]))


def load_google_credentials():
    """Load and refresh OAuth credentials from token.pickle without interactive browser prompts."""
    from google.auth.transport.requests import Request
    restore_google_secrets()
    if not os.path.exists("token.pickle"):
        return None
    with open("token.pickle", "rb") as f:
        creds = pickle.load(f)
    if not creds.valid and getattr(creds, "refresh_token", None):
        creds.refresh(Request())
        with open("token.pickle", "wb") as f:
            pickle.dump(creds, f)
    return creds if creds.valid else None


def _auth_headers(creds):
    from google.auth.transport.requests import Request
    if not creds.valid and getattr(creds, "refresh_token", None):
        creds.refresh(Request())
    return {"Authorization": f"Bearer {creds.token}"}


def _yt_analytics_query(creds, params):
    """Execute a YouTube Analytics API v2 reports query via REST."""
    import requests
    resp = requests.get(YT_ANALYTICS_URL, params=params, headers=_auth_headers(creds), timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"YouTube Analytics HTTP {resp.status_code}: {resp.text[:250]}")
    return resp.json()


def _yt_data_query(creds, url, params):
    """Execute a YouTube Data API v3 GET request via REST."""
    import requests
    resp = requests.get(url, params=params, headers=_auth_headers(creds), timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"YouTube Data API HTTP {resp.status_code}: {resp.text[:250]}")
    return resp.json()


def parse_iso8601_duration(duration_str):
    """Parse ISO8601 video duration (e.g. 'PT27S', 'PT1M5S') into integer seconds."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_str or "")
    if not m:
        return 0
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)


def analyze_retention_curve(rows, duration_sec):
    """
    Analyze 100-point YouTube retention curve (`elapsedVideoTimeRatio` vs `audienceWatchRatio`).
    Returns dict with hook hold, midpoint hold, tail hold, loop replay spike,
    half-audience drop second, and steepest single drop-off moment.
    """
    if not rows or duration_sec <= 0:
        return None

    points = []
    for row in rows:
        elapsed = float(row[0])
        watch = float(row[1])
        rel = float(row[2]) if len(row) > 2 else None
        sec = elapsed * duration_sec
        points.append((elapsed, sec, watch, rel))

    target_3s_ratio = min(1.0, 3.0 / max(duration_sec, 1))
    closest_3s = min(points, key=lambda p: abs(p[0] - target_3s_ratio))
    mid_points = [p for p in points if p[0] >= 0.50]
    tail_points = [p for p in points if p[0] >= 0.90]
    start_points = [p for p in points if p[0] <= 0.05]

    half_drop_sec = None
    for _, sec, watch, _ in points:
        if watch < 0.50:
            half_drop_sec = max(1, round(sec))
            break

    # Find steepest single-step drop-off after second 1 and before final 2s fade
    steepest_drop = 0.0
    steepest_drop_sec = None
    for idx in range(1, len(points)):
        prev_e, prev_s, prev_w, _ = points[idx - 1]
        cur_e, cur_s, cur_w, _ = points[idx]
        if cur_s < 1.5 or cur_s > max(2.0, duration_sec - 2.0):
            continue
        drop = prev_w - cur_w
        if drop > steepest_drop:
            steepest_drop = drop
            steepest_drop_sec = max(1, round(cur_s))

    rel_vals = [p[3] for p in points if p[3] is not None]

    return {
        "duration_sec": duration_sec,
        "hook_hold_3s_pct": round(closest_3s[2] * 100.0, 1),
        "midpoint_hold_pct": round(mid_points[0][2] * 100.0, 1) if mid_points else 0.0,
        "tail_hold_90_pct": round(tail_points[0][2] * 100.0, 1) if tail_points else 0.0,
        "loop_replay_spike_pct": round(max(p[2] for p in start_points) * 100.0, 1) if start_points else 0.0,
        "half_audience_sec": half_drop_sec,
        "steepest_drop_sec": steepest_drop_sec,
        "steepest_drop_pct": round(steepest_drop * 100.0, 1),
        "relative_retention_avg": round(statistics.mean(rel_vals), 3) if rel_vals else None,
    }


def detect_saturated_themes(titles, window=12, min_repeats=2):
    """
    Detect overused topic nouns/clichés in recent videos (e.g. 'brine', 'vortex', 'island', 'boat')
    so the script generator stops producing minor variations of the exact same subject.
    """
    recent = [str(t) for t in titles[-window:] if t]
    token_counts = Counter()
    for title in recent:
        # Clean unicode hyphens & hashtags
        normalized = title.replace("\u2011", "-").replace("\u2013", "-")
        raw_words = re.findall(r"[a-z]{3,}", normalized.lower())
        unique_in_title = {w for w in raw_words if w not in STOP_TOKENS}
        for w in unique_in_title:
            token_counts[w] += 1

    saturated = [word for word, count in token_counts.most_common(12) if count >= min_repeats]
    if recent and token_counts:
        top3_repeats = sum(c for _, c in token_counts.most_common(3))
        # High concentration of same nouns across recent titles lowers diversity sharply
        concentration = top3_repeats / max(len(recent), 1)
        diversity_score = max(0, min(100, round(100.0 - concentration * 42.0)))
    else:
        diversity_score = 100
    return saturated, diversity_score


def load_json_safe(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_active_strategy():
    """Return cached channel_strategy.json if available, else empty dict."""
    return load_json_safe(STRATEGY_FILE, {})


def is_weekly_analysis_due(strategy=None, interval_days=WEEKLY_INTERVAL_DAYS):
    """Check whether 7 days have elapsed since the last full dashboard analysis."""
    strat = strategy if strategy is not None else load_active_strategy()
    last_ts = strat.get("last_analyzed_at")
    if not last_ts:
        return True
    try:
        last_dt = datetime.datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
        return (now - last_dt).total_seconds() >= interval_days * 86400
    except Exception:
        return True


def _pick_fresh_archetype(saturated_words, recent_titles, current_archetype_id=None):
    """Rotate across high-retention archetypes while avoiding recently repeated clichés."""
    joined_recent = " ".join(recent_titles[-6:]).lower()
    scores = []
    for arch in CONTENT_ARCHETYPES:
        penalty = 0
        if arch["id"] == current_archetype_id:
            penalty += 2
        if arch["id"] == "forbidden_secrets" and any(w in joined_recent for w in ("island", "boat", "cavern", "forbidden")):
            penalty += 5
        if arch["id"] == "chilling_mysteries" and any(w in joined_recent for w in ("brine", "vortex", "abyss", "roar")):
            penalty += 3
        scores.append((penalty, arch))
    scores.sort(key=lambda item: item[0])
    return scores[0][1]


def _score_video_row(row, max_views, max_watch):
    """Composite viral score balancing high seen (views), watch time, retention %, and subs."""
    views, watch_min, avg_dur, avg_pct, likes, comments, shares, subs = row["metrics"]
    norm_views = (views / max(max_views, 1)) * 100.0
    norm_watch = (watch_min / max(max_watch, 1)) * 100.0
    norm_ret = min(130.0, avg_pct)
    eng_bonus = min(15.0, ((likes + comments * 2 + shares * 3) / max(views, 1)) * 200.0)
    sub_bonus = subs * 12.0
    return round(0.35 * norm_views + 0.30 * norm_watch + 0.25 * norm_ret + eng_bonus + sub_bonus, 2)


def run_weekly_dashboard_analysis(force=False, send_telegram=True):
    """
    Perform full YouTube Analytics Dashboard audit and persist `channel_strategy.json`
    plus enriched `performance_report.json`. Runs once per week unless `force=True`.
    """
    existing = load_active_strategy()
    if not force and not is_weekly_analysis_due(existing):
        print(
            f"ℹ️ Weekly YouTube dashboard analysis still fresh "
            f"(last run: {existing.get('last_analyzed_at')}). Next due: {existing.get('next_due_at')}."
        )
        return existing

    creds = load_google_credentials()
    if not creds:
        raise RuntimeError("Missing or invalid Google OAuth credentials (token.pickle).")

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    today = now_utc.date()
    d7_start = (today - datetime.timedelta(days=7)).isoformat()
    d14_start = (today - datetime.timedelta(days=14)).isoformat()
    d7_end_prev = (today - datetime.timedelta(days=8)).isoformat()
    d28_start = (today - datetime.timedelta(days=28)).isoformat()
    d42_start = (today - datetime.timedelta(days=42)).isoformat()
    end_str = today.isoformat()

    print("📊 [Weekly Dashboard Audit] Querying YouTube Analytics API v2 & Data API v3...")

    # 1. Channel-level 7-day metrics vs previous 7 days
    def _fetch_window_kpis(s_date, e_date):
        data = _yt_analytics_query(creds, {
            "ids": "channel==MINE",
            "startDate": s_date,
            "endDate": e_date,
            "metrics": "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,likes,shares",
        })
        rows = data.get("rows") or [[0, 0, 0, 0.0, 0, 0, 0]]
        r = rows[0]
        return {
            "views": int(r[0]),
            "watch_minutes": int(r[1]),
            "avg_view_duration_sec": int(r[2]),
            "avg_view_percentage": round(float(r[3]), 2),
            "subscribers_gained": int(r[4]),
            "likes": int(r[5]),
            "shares": int(r[6]),
        }

    kpis_7d = _fetch_window_kpis(d7_start, end_str)
    kpis_prev_7d = _fetch_window_kpis(d14_start, d7_end_prev)

    # Channel totals (Data API v3)
    chan_stats = {}
    try:
        ch_res = _yt_data_query(creds, YT_CHANNELS_URL, {"part": "snippet,statistics", "mine": "true"})
        if ch_res.get("items"):
            st = ch_res["items"][0].get("statistics", {})
            chan_stats = {
                "channel_title": ch_res["items"][0]["snippet"].get("title", ""),
                "total_views": int(st.get("viewCount", 0)),
                "subscribers": int(st.get("subscriberCount", 0)),
                "video_count": int(st.get("videoCount", 0)),
            }
    except Exception as e:
        print(f"⚠️ Channel Data v3 stats skipped: {e}")

    # 2. Top videos by Watch Time AND Top by High Seen (Views) over last 28 days
    video_rows_map = {}
    for sort_metric in ("-estimatedMinutesWatched", "-views"):
        rep = _yt_analytics_query(creds, {
            "ids": "channel==MINE",
            "startDate": d28_start,
            "endDate": end_str,
            "metrics": "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained",
            "dimensions": "video",
            "sort": sort_metric,
            "maxResults": 25,
        })
        for row in rep.get("rows", []):
            vid_id = row[0]
            video_rows_map[vid_id] = {
                "video_id": vid_id,
                "metrics": [
                    int(row[1]), int(row[2]), int(row[3]), float(row[4]),
                    int(row[5]), int(row[6]), int(row[7]), int(row[8]),
                ],
            }

    # Join video metadata (title, duration_sec, publishedAt)
    all_ids = list(video_rows_map.keys())
    video_records = []
    if all_ids:
        for i in range(0, len(all_ids), 50):
            batch_ids = ",".join(all_ids[i:i + 50])
            meta_res = _yt_data_query(creds, YT_VIDEOS_URL, {
                "part": "snippet,contentDetails,statistics",
                "id": batch_ids,
            })
            for item in meta_res.get("items", []):
                vid = item["id"]
                if vid not in video_rows_map:
                    continue
                m = video_rows_map[vid]["metrics"]
                dur_sec = parse_iso8601_duration(item["contentDetails"].get("duration", ""))
                pub_at = item["snippet"].get("publishedAt", "")
                video_records.append({
                    "video_id": vid,
                    "title": item["snippet"].get("title", ""),
                    "published_at": pub_at,
                    "duration_sec": dur_sec,
                    "metrics": m,
                    "views": m[0],
                    "watch_minutes": m[1],
                    "avg_view_duration_sec": m[2],
                    "avg_view_percentage": round(m[3], 2),
                    "likes": m[4],
                    "comments": m[5],
                    "shares": m[6],
                    "subscribers_gained": m[7],
                })

    max_views = max((v["views"] for v in video_records), default=1)
    max_watch = max((v["watch_minutes"] for v in video_records), default=1)
    for v in video_records:
        v["viral_score"] = _score_video_row(v, max_views, max_watch)

    top_by_viral = sorted(video_records, key=lambda x: x["viral_score"], reverse=True)
    top_by_views = sorted(video_records, key=lambda x: x["views"], reverse=True)
    top_by_watch = sorted(video_records, key=lambda x: x["watch_minutes"], reverse=True)
    qualified_ret = [v for v in video_records if v["views"] >= 25] or video_records
    top_by_retention = sorted(qualified_ret, key=lambda x: x["avg_view_percentage"], reverse=True)
    weakest = sorted(
        [v for v in video_records if v["views"] >= 10] or video_records,
        key=lambda x: (x["avg_view_percentage"], x["watch_minutes"]),
    )

    # 3. Retention Drop-Off Curve Analysis (Top 3 Winners vs Weakest / Recent)
    top_curves = []
    for v in top_by_viral[:3]:
        if v["duration_sec"] <= 0:
            continue
        try:
            c_rep = _yt_analytics_query(creds, {
                "ids": "channel==MINE",
                "startDate": d28_start,
                "endDate": end_str,
                "metrics": "audienceWatchRatio,relativeRetentionPerformance",
                "dimensions": "elapsedVideoTimeRatio",
                "filters": f"video=={v['video_id']}",
                "sort": "elapsedVideoTimeRatio",
            })
            parsed = analyze_retention_curve(c_rep.get("rows", []), v["duration_sec"])
            if parsed:
                parsed["video_id"] = v["video_id"]
                parsed["title"] = v["title"]
                top_curves.append(parsed)
        except Exception as e:
            print(f"⚠️ Curve fetch skipped for winner {v['video_id']}: {e}")

    weak_curves = []
    seen_top_ids = {c["video_id"] for c in top_curves}
    for v in weakest[:4]:
        if v["video_id"] in seen_top_ids or v["duration_sec"] <= 0:
            continue
        try:
            c_rep = _yt_analytics_query(creds, {
                "ids": "channel==MINE",
                "startDate": d28_start,
                "endDate": end_str,
                "metrics": "audienceWatchRatio,relativeRetentionPerformance",
                "dimensions": "elapsedVideoTimeRatio",
                "filters": f"video=={v['video_id']}",
                "sort": "elapsedVideoTimeRatio",
            })
            parsed = analyze_retention_curve(c_rep.get("rows", []), v["duration_sec"])
            if parsed:
                parsed["video_id"] = v["video_id"]
                parsed["title"] = v["title"]
                weak_curves.append(parsed)
            if len(weak_curves) >= 3:
                break
        except Exception:
            pass

    winner_3s = round(statistics.mean(c["hook_hold_3s_pct"] for c in top_curves), 1) if top_curves else None
    weak_3s = round(statistics.mean(c["hook_hold_3s_pct"] for c in weak_curves), 1) if weak_curves else None
    drop_secs = [c["steepest_drop_sec"] for c in (weak_curves + top_curves) if c.get("steepest_drop_sec")]
    half_secs = [c["half_audience_sec"] for c in weak_curves if c.get("half_audience_sec")]
    critical_dropoff_sec = round(statistics.median(half_secs or drop_secs)) if (half_secs or drop_secs) else 4
    steepest_drop_sec = round(statistics.median(drop_secs)) if drop_secs else 5

    winner_durs = [v["duration_sec"] for v in top_by_retention[:5] if 10 <= v["duration_sec"] <= 55]
    recommended_sec = max(18, min(25, round(statistics.median(winner_durs)))) if winner_durs else 22
    recommended_min_words = max(45, round(recommended_sec * 2.25))
    recommended_max_words = min(65, round(recommended_sec * 2.65))

    # 4. Traffic Sources (28 days)
    traffic_sources = []
    try:
        tr = _yt_analytics_query(creds, {
            "ids": "channel==MINE",
            "startDate": d28_start,
            "endDate": end_str,
            "metrics": "views,estimatedMinutesWatched",
            "dimensions": "insightTrafficSourceType",
            "sort": "-views",
        })
        for src_name, v_cnt, w_min in tr.get("rows", []):
            traffic_sources.append({
                "source": src_name,
                "views": int(v_cnt),
                "watch_minutes": int(w_min),
            })
    except Exception as e:
        print(f"⚠️ Traffic sources query skipped: {e}")

    # 5. Peak Weekday Performance (42 days)
    weekday_buckets = {d: {"views": [], "watch": [], "ret": []} for d in
                       ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
    try:
        day_rep = _yt_analytics_query(creds, {
            "ids": "channel==MINE",
            "startDate": d42_start,
            "endDate": end_str,
            "metrics": "views,estimatedMinutesWatched,averageViewPercentage",
            "dimensions": "day",
            "sort": "day",
        })
        for day_str, d_views, d_watch, d_pct in day_rep.get("rows", []):
            wd = datetime.date.fromisoformat(day_str).strftime("%A")
            weekday_buckets[wd]["views"].append(int(d_views))
            weekday_buckets[wd]["watch"].append(int(d_watch))
            weekday_buckets[wd]["ret"].append(float(d_pct))
    except Exception as e:
        print(f"⚠️ Daily performance query skipped: {e}")

    weekday_scores = []
    for wd, b in weekday_buckets.items():
        if b["views"]:
            avg_v = statistics.mean(b["views"])
            avg_w = statistics.mean(b["watch"])
            avg_r = statistics.mean(b["ret"])
            score = round(avg_v + avg_w * 4.0 + avg_r * 1.5, 1)
            weekday_scores.append({
                "day": wd,
                "avg_views": round(avg_v, 1),
                "avg_watch_minutes": round(avg_w, 1),
                "avg_retention_pct": round(avg_r, 1),
                "score": score,
            })
    weekday_scores.sort(key=lambda x: x["score"], reverse=True)
    peak_days = [w["day"] for w in weekday_scores[:3]] if weekday_scores else ["Friday", "Saturday", "Sunday"]
    low_days = [w["day"] for w in weekday_scores[-2:]] if len(weekday_scores) >= 5 else []

    # 6. Topic Saturation / Anti-Repetition Audit
    history = load_json_safe(TOPIC_HISTORY_FILE, [])
    recent_titles = [h.get("title", "") for h in history if h.get("title")] + [v["title"] for v in video_records[:10]]
    saturated_themes, diversity_score = detect_saturated_themes(recent_titles, window=14, min_repeats=2)
    chosen_archetype = _pick_fresh_archetype(saturated_themes, recent_titles, existing.get("active_archetype", {}).get("id"))

    # 7. Breakout Detection & Dynamic Trigger Decision
    recent_7d_cutoff = (now_utc - datetime.timedelta(days=7)).isoformat()
    median_views = statistics.median([v["views"] for v in video_records]) if video_records else 100
    recent_videos = [v for v in video_records if v.get("published_at", "") >= recent_7d_cutoff]
    breakout_winner = None
    for v in sorted(recent_videos or top_by_viral[:3], key=lambda x: x["viral_score"], reverse=True):
        is_high_seen = v["views"] >= max(220, 1.5 * median_views)
        is_high_ret = v["avg_view_percentage"] >= 85.0 and v["views"] >= max(60, 0.5 * median_views)
        if is_high_seen or is_high_ret:
            breakout_winner = v
            break

    breakout_trigger = False
    breakout_reason = ""
    if breakout_winner:
        # Only queue automatic sequel/bonus trigger if the core subject isn't already fatigued
        title_words = set(re.findall(r"[a-z]{3,}", breakout_winner["title"].lower())) - STOP_TOKENS
        overlap_with_ban = title_words & set(saturated_themes[:4])
        if not overlap_with_ban:
            breakout_trigger = True
            breakout_reason = (
                f"Breakout video '{breakout_winner['title']}' hit "
                f"{breakout_winner['views']} views, {breakout_winner['watch_minutes']}m watch time, "
                f"{breakout_winner['avg_view_percentage']}% APV"
            )
            # Queue fresh related-angle prompt into viewer_requests.json if empty
            existing_reqs = load_json_safe(VIEWER_REQUESTS_FILE, [])
            if not existing_reqs:
                spinoff_prompt = (
                    f"Create a fresh spin-off in the '{chosen_archetype['label']}' style inspired by why "
                    f"'{breakout_winner['title']}' succeeded (high tension + compact {recommended_sec}s pacing), "
                    f"using a completely NEW story."
                )
                with open(VIEWER_REQUESTS_FILE, "w", encoding="utf-8") as f:
                    json.dump([spinoff_prompt], f, indent=2)
        else:
            breakout_reason = (
                f"Top performer '{breakout_winner['title']}' flagged, but theme tokens {sorted(overlap_with_ban)} "
                f"are saturated; pivoting to '{chosen_archetype['label']}' instead."
            )

    cadence_mode = "surge" if breakout_trigger else "balanced"
    if diversity_score < 45:
        cadence_mode = "fresh_pivot"

    next_due = (now_utc + datetime.timedelta(days=WEEKLY_INTERVAL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    analyzed_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _compact_vid(v):
        return {
            "video_id": v["video_id"],
            "title": v["title"],
            "views": v["views"],
            "watch_minutes": v["watch_minutes"],
            "avg_view_duration_sec": v["avg_view_duration_sec"],
            "avg_view_percentage": v["avg_view_percentage"],
            "duration_sec": v["duration_sec"],
            "subscribers_gained": v["subscribers_gained"],
            "viral_score": v["viral_score"],
        }

    strategy = {
        "last_analyzed_at": analyzed_at,
        "next_due_at": next_due,
        "channel": chan_stats,
        "weekly_kpis": {
            "current_7d": kpis_7d,
            "previous_7d": kpis_prev_7d,
            "views_delta_pct": round(((kpis_7d["views"] - kpis_prev_7d["views"]) / max(kpis_prev_7d["views"], 1)) * 100.0, 1),
            "watch_minutes_delta_pct": round(((kpis_7d["watch_minutes"] - kpis_prev_7d["watch_minutes"]) / max(kpis_prev_7d["watch_minutes"], 1)) * 100.0, 1),
        },
        "top_high_seen": [_compact_vid(v) for v in top_by_views[:5]],
        "top_watch_time": [_compact_vid(v) for v in top_by_watch[:5]],
        "top_retention": [_compact_vid(v) for v in top_by_retention[:5]],
        "weakest_performers": [_compact_vid(v) for v in weakest[:5]],
        "retention_insights": {
            "winner_3s_hook_hold_pct": winner_3s,
            "weak_3s_hook_hold_pct": weak_3s,
            "steepest_drop_sec": steepest_drop_sec,
            "critical_dropoff_sec": critical_dropoff_sec,
            "recommended_target_sec": recommended_sec,
            "recommended_word_range": [recommended_min_words, recommended_max_words],
            "top_winner_curves": top_curves,
            "weak_curves": weak_curves,
        },
        "traffic_sources_28d": traffic_sources,
        "weekday_performance": weekday_scores,
        "topic_diversity": {
            "diversity_score": diversity_score,
            "saturated_themes_to_ban": saturated_themes,
        },
        "active_archetype": chosen_archetype,
        "trigger_policy": {
            "cadence_mode": cadence_mode,
            "peak_days": peak_days,
            "low_days": low_days,
            "primary_slot_utc": "11:15",   # 4:45 PM IST prime pre-evening surge
            "secondary_slot_utc": "05:45", # 11:15 AM IST midday slot
            "daily_slots_ist": [[11, 15], [16, 45]],
            "breakout_bonus_dispatch": breakout_trigger,
            "breakout_reason": breakout_reason,
        },
    }

    with open(STRATEGY_FILE, "w", encoding="utf-8") as f:
        json.dump(strategy, f, indent=2)

    # Also update performance_report.json with real live dashboard diagnostics
    perf_report = {
        "generated_at": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "channel_summary": chan_stats,
        "weekly_kpis": strategy["weekly_kpis"],
        "top_videos_this_week": [
            {
                "title": v["title"],
                "views": str(v["views"]),
                "watch_minutes": str(v["watch_minutes"]),
                "avg_view_percentage": f"{v['avg_view_percentage']}%",
                "likes": str(v["likes"]),
            }
            for v in top_by_watch[:10]
        ],
        "diagnostic_checks": {
            "high_seen_leader": f"{top_by_views[0]['title']} ({top_by_views[0]['views']} views)" if top_by_views else "N/A",
            "watch_time_leader": f"{top_by_watch[0]['title']} ({top_by_watch[0]['watch_minutes']} min)" if top_by_watch else "N/A",
            "retention_leader": f"{top_by_retention[0]['title']} ({top_by_retention[0]['avg_view_percentage']}% APV)" if top_by_retention else "N/A",
            "critical_dropoff_second": f"{critical_dropoff_sec}s (Top 3s hold: {winner_3s}% vs Weak: {weak_3s}%)",
            "peak_days": ", ".join(peak_days),
            "topic_diversity": f"{diversity_score}/100 (Banning saturated: {', '.join(saturated_themes[:6]) or 'None'})",
            "next_recommended_niche": chosen_archetype["label"],
            "trigger_mode": f"{cadence_mode} (Breakout Dispatch: {breakout_trigger})",
        },
    }
    with open(PERFORMANCE_REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(perf_report, f, indent=2)

    print(f"✅ Weekly dashboard audit saved to '{STRATEGY_FILE}' and '{PERFORMANCE_REPORT_FILE}'.")
    print(format_strategy_summary(strategy))

    if send_telegram:
        send_weekly_telegram_digest(strategy)

    return strategy


def format_strategy_summary(strategy):
    """Build a concise Markdown summary of the weekly analytics audit."""
    if not strategy:
        return "⚠️ No weekly strategy data available yet."
    k7 = strategy.get("weekly_kpis", {}).get("current_7d", {})
    deltas = strategy.get("weekly_kpis", {})
    ret = strategy.get("retention_insights", {})
    tp = strategy.get("trigger_policy", {})
    div = strategy.get("topic_diversity", {})
    arch = strategy.get("active_archetype", {})
    top_v = (strategy.get("top_high_seen") or [{}])[0]
    top_r = (strategy.get("top_retention") or [{}])[0]

    words_rng = ret.get("recommended_word_range", [50, 62])
    saturated = ", ".join(div.get("saturated_themes_to_ban", [])[:6]) or "None"
    peak_days = ", ".join(tp.get("peak_days", []))
    sub_rate = round((k7.get("subscribers_gained", 0) / max(k7.get("views", 1), 1)) * 1000.0, 2)
    longform_candidate = top_r if top_r.get("avg_view_percentage", 0) >= 85 else top_v

    lines = [
        "📊 *WEEKLY YOUTUBE DASHBOARD AUDIT*",
        f"• *7-Day Views:* {k7.get('views', 0):,} ({deltas.get('views_delta_pct', 0):+.1f}% WoW)",
        f"• *Watch Time:* {k7.get('watch_minutes', 0):,} min ({deltas.get('watch_minutes_delta_pct', 0):+.1f}% WoW)",
        f"• *Avg Retention:* {k7.get('avg_view_percentage', 0)}% ({k7.get('avg_view_duration_sec', 0)}s avg)",
        f"• *Subs Gained (7d):* +{k7.get('subscribers_gained', 0)} (`{sub_rate}` subs / 1k views)",
        "",
        "🏆 *28-Day Leaders:*",
        f"• *Most Seen:* {top_v.get('title', 'N/A')} ({top_v.get('views', 0)} views)",
        f"• *Best Retention:* {top_r.get('title', 'N/A')} ({top_r.get('avg_view_percentage', 0)}% APV)",
        f"• *Long-Form Candidate:* {longform_candidate.get('title', 'N/A')} (Ready in `longform_expansion_ideas.json`)",
        "",
        "📉 *Retention Curve Updates:*",
        f"• *3s Hook Hold:* Winners `{ret.get('winner_3s_hook_hold_pct')}%` vs Weak `{ret.get('weak_3s_hook_hold_pct')}%`",
        f"• *Drop-off Guard:* Steepest drop ~`{ret.get('steepest_drop_sec', 5)}s` · 50% bail ~`{ret.get('critical_dropoff_sec', 9)}s`",
        f"• *Sweet-Spot Target:* `{ret.get('recommended_target_sec')}s` (`{words_rng[0]}-{words_rng[1]}` words)",
        "",
        "🎯 *Topic & Trigger Updates:*",
        f"• *Next Niche Focus:* {arch.get('label', 'N/A')}",
        f"• *Topic Diversity:* `{div.get('diversity_score', 100)}/100` (Muted repeats: `{saturated}`)",
        f"• *Peak Days (2x slots):* {peak_days}",
        f"• *Cadence Mode:* `{tp.get('cadence_mode', 'balanced')}` | Breakout Bonus: `{tp.get('breakout_bonus_dispatch', False)}`",
    ]
    return "\n".join(lines)


def send_weekly_telegram_digest(strategy):
    """Send the weekly analytics & strategy report to Telegram if configured."""
    import requests
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("ℹ️ Telegram weekly digest skipped (TELEGRAM_BOT_TOKEN/CHAT_ID not configured).")
        return
    try:
        msg = format_strategy_summary(strategy)
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=15,
        )
        if r.status_code != 200:
            # Retry without parse_mode in case a video title contains unescaped Markdown chars
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg.replace("*", "").replace("`", "")},
                timeout=15,
            )
        if r.status_code == 200:
            print("📲 Weekly dashboard report sent to Telegram!")
        else:
            print(f"⚠️ Telegram digest status {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ Telegram digest failed: {e}")


def should_trigger_generation(event_name="schedule", current_utc_hour=None, now_ist=None):
    """
    Decide whether a scheduled trigger slot should generate a video now based on
    weekly dashboard performance, peak days, breakout triggers, and queued demand.

    Rules:
      - Non-scheduled events (manual dispatch, push, CLI test) ALWAYS trigger.
      - Pending cliffhanger or viewer request ALWAYS triggers.
      - Peak Days or Surge Mode ALWAYS trigger both morning & evening slots.
      - Off-peak days run only the primary high-yield evening slot (11:15 UTC / 4:45 PM IST)
        unless local/cloud passes no slot restriction.
    """
    if event_name != "schedule":
        return True, f"Manual or code-push event ('{event_name}') bypasses schedule throttling."

    strategy = load_active_strategy()
    saturated = {w.lower() for w in strategy.get("topic_diversity", {}).get("saturated_themes_to_ban", [])[:6]}

    if os.path.exists(CLIFFHANGER_FILE):
        try:
            with open(CLIFFHANGER_FILE, "r", encoding="utf-8") as f:
                cliff_txt = f.read().lower()
            cliff_words = set(re.findall(r"[a-z]{3,}", cliff_txt))
            if not (cliff_words & saturated):
                return True, "Active fresh cliffhanger awaiting Part 2 resolution."
        except Exception:
            return True, "Active cliffhanger awaiting Part 2 resolution."

    reqs = load_json_safe(VIEWER_REQUESTS_FILE, [])
    if reqs:
        return True, "Pending high-demand/breakout viewer request queued."

    if not strategy:
        return True, "No weekly channel_strategy.json yet; allowing default trigger."

    tp = strategy.get("trigger_policy", {})
    if tp.get("breakout_bonus_dispatch"):
        # Honor breakout bonus for up to 24h after the weekly audit (or until cleared)
        try:
            analyzed_dt = datetime.datetime.fromisoformat(strategy["last_analyzed_at"].replace("Z", "+00:00"))
            age_hours = (datetime.datetime.now(datetime.timezone.utc) - analyzed_dt).total_seconds() / 3600.0
            if age_hours <= 24:
                return True, "Weekly analytics flagged a breakout winner spin-off trigger (within 24h surge window)."
        except Exception:
            pass

    utc_now = datetime.datetime.now(datetime.timezone.utc)
    if now_ist is None:
        ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        now_ist = datetime.datetime.now(ist_tz)

    today_name = now_ist.strftime("%A")
    peak_days = tp.get("peak_days", [])
    cadence_mode = tp.get("cadence_mode", "balanced")

    if cadence_mode == "surge" or today_name in peak_days:
        return True, f"{today_name} is a Peak/Surge day ({cadence_mode}); all daily slots active."

    # Off-peak day: focus on the primary pre-evening peak slot (around 10-14 UTC / 15-19 IST)
    utc_hour = current_utc_hour if current_utc_hour is not None else utc_now.hour
    if 9 <= utc_hour <= 15:
        return True, f"Primary daily high-yield slot on {today_name} (focusing quota on prime watch window)."

    return False, (
        f"{today_name} is an off-peak day (Peak days: {', '.join(peak_days)}); "
        f"skipping secondary morning slot (UTC {utc_hour:02d}:xx) to concentrate reach on prime slot."
    )


def _write_github_output(outputs):
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    with open(gh_out, "a", encoding="utf-8") as f:
        for k, v in outputs.items():
            f.write(f"{k}={v}\n")


def main():
    parser = argparse.ArgumentParser(description="YouTube Weekly Dashboard Analyzer & Trigger Engine")
    parser.add_argument("--weekly", action="store_true", help="Force full weekly dashboard audit now.")
    parser.add_argument("--if-due", action="store_true", help="Run weekly audit only if >= 7 days since last run.")
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram digest on weekly audit.")
    parser.add_argument("--check-trigger", action="store_true", help="Evaluate whether current slot should trigger video generation.")
    parser.add_argument("--event", default="schedule", help="Trigger event name (schedule, workflow_dispatch, push).")
    parser.add_argument("--utc-hour", type=int, default=None, help="Override UTC hour for testing trigger decision.")
    parser.add_argument("--summary", action="store_true", help="Print cached weekly strategy summary.")
    args = parser.parse_args()

    if args.weekly or args.if_due:
        strat = run_weekly_dashboard_analysis(force=args.weekly, send_telegram=not args.no_telegram)
        tp = strat.get("trigger_policy", {})
        _write_github_output({
            "breakout_trigger": "true" if tp.get("breakout_bonus_dispatch") else "false",
            "cadence_mode": tp.get("cadence_mode", "balanced"),
            "next_niche": strat.get("active_archetype", {}).get("id", "dark_psychology"),
        })
        return

    if args.check_trigger:
        should_run, reason = should_trigger_generation(event_name=args.event, current_utc_hour=args.utc_hour)
        print(f"🎯 Trigger Decision: should_run={should_run} | Reason: {reason}")
        _write_github_output({
            "should_run": "true" if should_run else "false",
            "reason": reason.replace("\n", " "),
        })
        return

    if args.summary:
        print(format_strategy_summary(load_active_strategy()))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
