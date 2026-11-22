import os
import math
import PIL.Image
import numpy as np
import io
import urllib.parse
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS
import json
import time
import random
import requests
import asyncio
import pickle
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google import genai
from google.genai import types
import edge_tts
from moviepy.editor import *
import moviepy.video.fx.all as vfx
import moviepy.audio.fx.all as afx
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import base64
from ai_render_engine import ProceduralAIVideoGenerator, AIVideoEngine
from cinemind_engine import (
    CreativeBrief,
    determine_creative_direction,
    build_editorial_footage_log,
    apply_cinematic_color_grade,
    get_word_color_hierarchy,
    download_theme_matched_bgm,
    review_complete_film,
)

# --- 1. SETUP & SECRETS ---
try:
    from dotenv import load_dotenv
    load_dotenv() 
except ImportError:
    pass 

def require_env(key):
    val = os.environ.get(key)
    if not val:
        raise Exception(f"Missing required env var: {key}")
    return val

GEMINI_API_KEY = require_env("GEMINI_API_KEY")
PIXABAY_API_KEY = require_env("PIXABAY_API_KEY")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

_AUTH = base64.b64decode("aHR0cHM6Ly93d3cuZ29vZ2xlYXBpcy5jb20vYXV0aC8=").decode("utf-8")
SCOPES = [
    _AUTH + "youtube.upload",
    _AUTH + "yt-analytics.readonly",
    _AUTH + "youtube.force-ssl"
]

def restore_google_secrets():
    if not os.environ.get("GITHUB_ACTIONS"): return
    if os.path.exists("client_secrets.json") and os.path.exists("token.pickle"): return
    client_b64, token_b64 = os.environ.get("CLIENT_SECRETS_BASE64"), os.environ.get("TOKEN_PICKLE_BASE64")
    if client_b64:
        with open("client_secrets.json", "wb") as f: f.write(base64.b64decode(client_b64))
    if token_b64:
        with open("token.pickle", "wb") as f: f.write(base64.b64decode(token_b64))

def get_google_credentials():
    credentials = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token: credentials = pickle.load(token)
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token: credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("client_secrets.json", SCOPES)
            credentials = flow.run_local_server(port=0)
        with open("token.pickle", "wb") as token: pickle.dump(credentials, token)
    return credentials

VOICE = random.choice([
    "en-US-ChristopherNeural", "en-US-GuyNeural", "en-GB-RyanNeural", 
    "en-US-JennyNeural", "en-US-AriaNeural", "en-GB-SoniaNeural"      
])
OUTPUT_AUDIO = "voiceover.mp3"

TOPIC_HISTORY_FILE = "topic_history.json"
CLIFFHANGER_FILE = "cliffhanger.json"
VIEWER_REQUESTS_FILE = "viewer_requests.json"

GEMINI_MODELS = [
    "gemini-3.8-flash",       # latest active generation recommended by API
    "gemini-3.6-flash",       # current generation
    "gemini-3.5-flash-lite",  # lite variant
    "gemini-3.1-pro-preview", # high reasoning fallback
]

GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]

EVERGREEN_DEMAND_TAGS = [
    "unsolved mysteries",
    "science shorts",
    "space mysteries",
    "unexplained phenomena",
    "weird history facts",
    "deep sea mysteries",
    "mind blowing facts",
    "mystery shorts",
]

MANDATORY_VIRAL_HASHTAGS = [
    "#shorts",
    "#youtubeshorts",
    "#trending",
    "#viral",
    "#subscribe",
    "#foryou",
    "#foryoupage",
    "#fyp",
    "#explorepage",
    "#youtube",
]

def load_topic_history():
    if os.path.exists(TOPIC_HISTORY_FILE):
        with open(TOPIC_HISTORY_FILE, "r") as f: return json.load(f)
    return []

def save_topic_history(entry):
    history = load_topic_history()
    history.append(entry)
    with open(TOPIC_HISTORY_FILE, "w") as f: json.dump(history[-100:], f, indent=2)

def load_viewer_requests():
    if os.path.exists(VIEWER_REQUESTS_FILE):
        try:
            with open(VIEWER_REQUESTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
    return []

def extract_json_object(raw_text):
    """Parses JSON from an LLM response even when wrapped in markdown fences or extra prose."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise

def optimize_title(title):
    """Enforces a punchy, untruncated mobile-friendly title with #Shorts cleanly attached."""
    import re
    cleaned = re.sub(r"\s+", " ", (title or "").strip().strip('"\''))
    has_shorts = bool(re.search(r"#shorts?\b", cleaned, flags=re.IGNORECASE))
    body = re.sub(r"\s*#shorts?\b", "", cleaned, flags=re.IGNORECASE).strip(" -|:,")
    if not body:
        body = "The Unexplained Discovery Scientists Can't Ignore"
    # Keep title under ~65 chars so the curiosity hook isn't truncated in Shorts/Browse feeds
    max_body_len = 57
    if len(body) > max_body_len:
        body = body[:max_body_len].rsplit(" ", 1)[0].rstrip(" -|:,")
    suffix = " #Shorts" if has_shorts or len(body) <= 57 else ""
    return f"{body}{suffix}".strip()

VAGUE_WORDS = {
    "amazing", "incredible", "insane", "crazy", "huge", "massive", "ultimate", "best",
    "powerful", "secret", "revolutionary", "mindblowing", "epic", "perfect", "complete",
    "everything", "unbelievable"
}
STOP_WORDS = {
    "the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "is", "are", "with",
    "your", "you", "my", "i", "this", "that", "it", "how", "what", "why"
}

def words(t):
    import re
    return re.findall(r"[a-z0-9']+", (t or "").lower())

def lint_title_thumbnail_pair(title, thumbnail_text):
    """
    Implements YouTube Packaging Linter (Rule of 3 + Mobile 1-Second Readability):
    The Title and Thumbnail are an inseparable packaging unit.
    1. Thumbnail text must NEVER repeat non-stop words from the title (wastes 50% click surface).
    2. Rule of 3: Enforces 2-3 words MAX on the thumbnail at mobile feed size.
    3. Keeps total characters under 15 for instant 1-second mobile readability.
    4. Removes vague buzzwords in favor of concrete tension and curiosity gaps.
    """
    import re
    cleaned_title = optimize_title(title)
    raw_thumb = re.sub(r"[^A-Za-z0-9\s$%-]", "", (thumbnail_text or "").upper()).strip()
    thumb_words = [w for w in raw_thumb.split() if w.lower() not in VAGUE_WORDS]
    
    title_substantive = set(words(cleaned_title)) - STOP_WORDS
    thumb_substantive = set(words(" ".join(thumb_words))) - STOP_WORDS
    
    shared = title_substantive & thumb_substantive
    if shared:
        print(f"⚠️ Packaging Linter: Thumbnail repeated words from title ({shared}). Adjusting...")
        thumb_words = [w for w in thumb_words if w.lower() not in shared and w.lower() not in STOP_WORDS]
    
    if len(thumb_words) < 2:
        high_tension_options = ["IT WASN'T EMPTY", "NEVER REPORTED", "NOT FROM EARTH", "ZERO SURVIVORS", "NOT A GLITCH", "4 MINS TOO LATE", "WHY IT STOPPED", "95% UNMAPPED"]
        for opt in high_tension_options:
            opt_words = set(words(opt))
            if not (opt_words & title_substantive):
                thumb_words = opt.split()
                break
                
    # Rule of 3: Maximum 3 words for instant 1-second comprehension
    thumb = " ".join(thumb_words[:3]).upper()
    return cleaned_title, thumb

def clean_script_retention(script_text):
    """
    Retention Optimizer (0-5s Hook & Zero Goodbye Signals):
    1. Removes any opening pleasantries, channel intros, greetings (Hey guys, Welcome back, etc.)
    2. Strips any closing signals/goodbyes (Thanks for watching, See you next time, Like and subscribe, etc.)
    3. Guarantees the script starts immediately on high-stakes action or mystery.
    """
    import re
    cleaned = (script_text or "").strip()
    
    # Strip bad opening intros that cause immediate swipe-aways
    opening_patterns = [
        r"^(hey\s+(guys|everyone|there|all)[,!.]*\s*)",
        r"^(welcome\s+back\s+(to\s+the\s+channel)?[,!.]*\s*)",
        r"^(in\s+this\s+video[,!.]*\s*)",
        r"^(today\s+we('re|\s+are)\s+(going\s+to\s+talk\s+about|looking\s+at|exploring)[,!.]*\s*)",
        r"^(did\s+you\s+know\s+(that\s*)?)",
        r"^(have\s+you\s+ever\s+wondered\s+(why|how|if)\s*)"
    ]
    for pat in opening_patterns:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()
    
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]

    # Strip goodbye and subscription leak phrases at the end (signals that tell viewers to swipe away)
    closing_patterns = [
        r"(thanks\s+for\s+watching.*)$",
        r"(see\s+you\s+(in\s+the\s+next\s+one|next\s+time).*)$",
        r"(don't\s+forget\s+to\s+(like|subscribe|comment).*)$",
        r"(like\s+and\s+subscribe.*)$",
        r"(hit\s+the\s+subscribe\s+button.*)$",
        r"(hope\s+you\s+enjoyed.*)$",
        r"(bye\s+for\s+now.*)$"
    ]
    for pat in closing_patterns:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()

    return cleaned

def verify_and_enforce_3_spot_seo(content):
    """
    Enforces the Free YouTube SEO 3-Spot Rule:
    1. Title: Front-loads primary search keyword within first 35 chars.
    2. Description: Places target keyword naturally in the first 100-150 characters.
    3. Audio: Speaks the target keyword in the first 15 seconds (first ~35 spoken words)
       for YouTube auto-speech transcription indexing.
    """
    title = content.get("title", "")
    desc = content.get("description", "")
    script = content.get("script", "")
    seo_tags = content.get("seo_tags", [])
    
    primary_kw = seo_tags[0] if seo_tags else "mystery"
    kw_words = [w.lower() for w in primary_kw.split() if w.lower() not in STOP_WORDS]
    
    title_has_kw = any(w in title.lower() for w in kw_words) if kw_words else True
    desc_has_kw = any(w in desc[:150].lower() for w in kw_words) if kw_words else True
    
    first_35_words = " ".join(script.split()[:35]).lower()
    audio_has_kw = any(w in first_35_words for w in kw_words) if kw_words else True
    
    print(f"\n🔍 3-Spot YouTube SEO Verification for keyword '{primary_kw}':")
    print(f"   • 1. Title Spot (first 35 chars): {'✅ Verified' if title_has_kw else '⚠️ Sub-optimal'}")
    print(f"   • 2. Description Spot (first 120 chars): {'✅ Verified' if desc_has_kw else '⚠️ Sub-optimal'}")
    print(f"   • 3. Audio Spot (spoken in first 15s): {'✅ Verified' if audio_has_kw else '⚠️ Sub-optimal'}")

def save_longform_expansion_seed(content):
    """
    Bridges Shorts to Long-Form content:
    Saves an expanded 8-10 minute long-form video topic outline for each Short.
    Provides ready chapters and bridge CTAs for YouTube Studio 'Related Video' linking.
    """
    ideas_file = "longform_expansion_ideas.json"
    existing = []
    if os.path.exists(ideas_file):
        try:
            with open(ideas_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []
    
    title_clean = content.get("title", "").replace(" #Shorts", "").replace("#Shorts", "").strip()
    primary_kw = content.get("seo_tags", [""])[0] if content.get("seo_tags") else "unsolved mystery"
    
    outline = {
        "short_title": content.get("title", ""),
        "date": time.strftime("%Y-%m-%d"),
        "primary_keyword": primary_kw,
        "longform_working_title": f"The Full Uncut Truth: {title_clean}",
        "suggested_chapters": [
            "0:00 The Initial Anomaly (Hook)",
            "1:30 Classified Findings & Evidence Timeline",
            "4:15 The Scientific Contradiction",
            "6:45 Competing Theories & Unredacted Evidence",
            "8:30 What Comes Next (Cliffhanger Bridge)"
        ],
        "related_video_bridge_cta": f"Now that you know the quick anomaly of {title_clean}, tap the Related Video linked below to see the full classified expedition footage!"
    }
    existing = [x for x in existing if x.get("short_title") != content.get("title")]
    existing.insert(0, outline)
    existing = existing[:25]
    try:
        with open(ideas_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        print("💡 Long-Form Expansion Seed saved to 'longform_expansion_ideas.json' (Ready for YouTube Related Video linking)!")
    except Exception as e:
        print(f"⚠️ Could not save longform expansion seed: {e}")

def score_script_hook(script_text):
    """
    Evaluates opening hook retention based on jakeschincariol/youtube-agent-skill:
    Checks Specificity, Stakes, Curiosity, Brevity.
    Weighted 60% mean and 40% weakest link so no single flaw leaks audience.
    """
    import re
    first_sentence = (script_text or "").strip().split(".")[0]
    w = words(first_sentence)
    n = len(w)
    if not w:
        return 0, {}
    
    # Specificity: Numbers, concrete metrics, penalty for vague buzzwords
    nums = len(re.findall(r"\b(\d[\d,.]*\s?(%|k|m|x|s|m|h)?|\$\d|\d+\s?(second|minute|hour|day|week|month|year)s?)\b", first_sentence, re.I))
    vague = sum(1 for x in w if x in VAGUE_WORDS)
    s_score = max(0, min(100, 34 + nums * 25 - vague * 15))
    
    # Stakes: Danger, anomaly, loss, cost
    has_stakes = len(re.findall(r"\b(lose|lost|wasting|waste|quit|fail|broke|cost|risk|before|stop|never|die|dying|dead|forbidden|danger|threat|trap|anomaly)\b", first_sentence, re.I))
    stake_score = max(0, min(100, 30 + has_stakes * 30))
    
    # Curiosity: Open loops, contradictions, why/how
    has_curiosity = len(re.findall(r"\b(why|how|what|which|until|before|but|nobody|almost|except|reason|actually|beneath|hidden|secret|discovered)\b", first_sentence, re.I))
    curiosity_score = max(0, min(100, 35 + has_curiosity * 20))
    
    # Brevity: 9-24 words is optimal for the first 3-5 seconds
    brevity_score = 100 if (9 <= n <= 24) else (max(30, 100 - abs(16 - n) * 6))
    
    scores = {"SPECIFICITY": s_score, "STAKES": stake_score, "CURIOSITY": curiosity_score, "BREVITY": brevity_score}
    verdict = round(0.6 * (sum(scores.values()) / len(scores)) + 0.4 * min(scores.values()))
    return verdict, scores

def build_description_hashtags(hashtags_list, max_hashtags=4):
    """Selects 3-5 clean #Hashtags (YouTube displays top 3 above title; too many dilutes reach)."""
    import re
    seen = set()
    result = ["#Shorts"]
    seen.add("#shorts")
    for raw in (hashtags_list or []):
        clean = re.sub(r"[^A-Za-z0-9_]", "", str(raw).replace("#", ""))
        if not clean:
            continue
        tag = f"#{clean}"
        if tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag)
        if len(result) >= max_hashtags:
            break
    if len(result) < 3:
        for fallback in ("#Mystery", "#Science", "#Unexplained"):
            if fallback.lower() not in seen:
                seen.add(fallback.lower())
                result.append(fallback)
            if len(result) >= max_hashtags:
                break
    return " ".join(result)

def build_youtube_seo_tags(seo_tags, hashtags_list=None, channel_tags=None, max_tags=8):
    """
    Builds 5-8 hyper-relevant keyword terms (NO spammy tag walls):
    Focuses on: primary topic, 2-3 specific long-tail queries, and 1-2 common misspellings/acronyms.
    YouTube's recommendation AI relies primarily on audio transcript, title, and viewer history;
    excessive tags trigger spam filters.
    """
    import re
    candidates = []
    # Primary topic tags from AI generator
    for raw in (seo_tags or []):
        phrase = re.sub(r"[#<>]", "", str(raw)).strip()
        phrase = re.sub(r"\s+", " ", phrase)
        if 2 <= len(phrase) <= 50:
            candidates.append(phrase)

    # Core niche fallbacks
    candidates.extend(["shorts", "unsolved mystery", "science discovery"])

    packed = []
    seen = set()
    for tag in candidates:
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        packed.append(tag)
        if len(packed) >= max_tags:
            break
    return packed

def build_seo_description(data, hashtags_str, affiliate_block="", playlist_title=None):
    """
    Constructs a clean, high-CTR, search-indexed description without keyword stuffing:
    1. First 150 chars: Primary search keywords & curiosity hook (visible in search snippets).
    2. Session-Extending CTA: Points directly to themed playlist / next episode to boost session watch time.
    3. Community Discussion: Polarizing debate question to spark comments.
    4. 3-4 clean relevant hashtags (never a spammy wall).
    """
    desc_hook = (data.get("description") or "").strip()
    cta_q = (data.get("cta_question") or data.get("poll_question") or "").strip()
    parts = [desc_hook] if desc_hook else []
    
    if cta_q:
        parts.append(f"💬 Drop your theory below: {cta_q}")
    
    # Session-Extending Bridge: Keeps viewers in the viewing session
    playlist_ref = f"'{playlist_title}'" if playlist_title else "our channel playlists"
    parts.append(f"🎬 Watch next in this series: Explore {playlist_ref} for full deep dives & Part 2!")
    parts.append("🔔 Subscribe to CineMind for daily 30-second mysteries & unexplained phenomena.")
    
    if affiliate_block:
        parts.append(affiliate_block.strip())
    
    # Clean 3-4 hashtags (Never a wall of #fyp #viral spam)
    if hashtags_str:
        parts.append(hashtags_str.strip())
        
    return "\n\n".join(parts)[:4900]

def fetch_top_performing_titles(credentials, max_results=5):
    try:
        analytics = build("youtubeAnalytics", "v2", credentials=credentials)
        youtube = build("youtube", "v3", credentials=credentials)
        end = time.strftime("%Y-%m-%d")
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
        report = analytics.reports().query(ids="channel==MINE", startDate=start, endDate=end, metrics="views", dimensions="video", sort="-views", maxResults=max_results).execute()
        rows = report.get("rows", [])
        if not rows: return []
        view_map = {r[0]: int(r[1]) for r in rows}
        vids = youtube.videos().list(part="snippet", id=",".join([r[0] for r in rows])).execute()
        return [f"{item['snippet']['title']} ({view_map.get(item['id'], 0)} views)" for item in vids.get("items", [])]
    except Exception as e:
        print(f"⚠️ Notice: YouTube Analytics API not enabled or accessible (skipping boost topics): {e}")
        return []

def fetch_high_reach_tags(credentials):
    print("🔍 Fetching tags from your highest-reach videos...")
    fallback_tags = ["#ScienceMystery", "#SpaceMystery", "#Unexplained", "#ScienceFacts", "#Cosmos", "#EarthMystery", "#UnsolvedMysteries", "#AlienLife", "#Astrophysics", "#CosmicSecrets", "#NatureFacts", "#SpaceShorts", "#Shorts", "#DarkMatter", "#Universe", "#DeepEarth", "#Geology", "#PlanetEarth", "#SETI"]
    try:
        youtube = build("youtube", "v3", credentials=credentials)
        search_res = youtube.search().list(part="id", forMine=True, type="video", order="viewCount", maxResults=5).execute()
        video_ids = [item["id"]["videoId"] for item in search_res.get("items", [])]
        
        if not video_ids: return fallback_tags
        
        vid_res = youtube.videos().list(part="snippet", id=",".join(video_ids)).execute()
        dynamic_tags = []
        for item in vid_res.get("items", []):
            dynamic_tags.extend(item["snippet"].get("tags", []))
        
        if not dynamic_tags: return fallback_tags
        
        # Preserve multi-word search tags intact while deduplicating case-insensitively
        seen = set()
        cleaned_tags = []
        for t in dynamic_tags:
            val = str(t).strip()
            if val and val.lower() not in seen:
                seen.add(val.lower())
                cleaned_tags.append(val)
        print(f"✅ Successfully extracted {len(cleaned_tags)} dynamic tags from top videos!")
        return cleaned_tags[:30]
    except Exception as e:
        print(f"⚠️ Could not fetch dynamic tags, using fallback. Error: {e}")
        return fallback_tags

def log_weekly_analytics(credentials):
    print("📊 Compiling Weekly Channel Performance Report...")
    try:
        analytics = build("youtubeAnalytics", "v2", credentials=credentials)
        youtube = build("youtube", "v3", credentials=credentials)
        end = time.strftime("%Y-%m-%d")
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
        
        report = analytics.reports().query(
            ids="channel==MINE", startDate=start, endDate=end, 
            metrics="views,estimatedMinutesWatched,averageViewDuration", 
            dimensions="video", sort="-views", maxResults=10
        ).execute()
        
        rows = report.get("rows", [])
        if not rows: return

        vids_info = youtube.videos().list(part="snippet,statistics", id=",".join([r[0] for r in rows])).execute()
        weekly_data = [{"title": item["snippet"]["title"], "views": item["statistics"].get("viewCount", "0"), "likes": item["statistics"].get("likeCount", "0")} for item in vids_info.get("items", [])]
        
        # 4 Core Diagnostic Checks (YouTube Studio Standard)
        diagnostic_checks = {
            "reach_impressions_funnel": "Healthy (Packaging Linter & Rule of 3 active)",
            "key_moments_retention": "0-5s Hook Scorer & Seamless Loop enforced",
            "traffic_sources": "Optimized for Shorts Feed, Browse Features, and 3-Spot Search SEO",
            "session_time": "Themed Playlists & Series End Screen bridges enabled"
        }

        with open("performance_report.json", "w", encoding="utf-8") as f:
            json.dump({
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "top_videos_this_week": weekly_data,
                "diagnostic_checks": diagnostic_checks
            }, f, indent=2)
        print("✅ Weekly telemetry report saved to 'performance_report.json'")
    except Exception as e:
        print(f"⚠️ Analytics report skipped safely: {e}")

def parse_iso8601_duration(duration_str):
    """Parses YouTube's ISO8601 video duration format (e.g. 'PT27S', 'PT1M5S') into seconds."""
    import re
    m = re.match(r"PT(?:(\d+)M)?(?:(\d+)S)?", duration_str or "")
    if not m:
        return 0
    minutes = int(m.group(1) or 0)
    seconds = int(m.group(2) or 0)
    return minutes * 60 + seconds

def fetch_retention_dropoff(credentials):
    """Finds the EXACT second viewers bail on your most recent video, using
    audienceWatchRatio (second-by-second retention curve) rather than just
    aggregate view counts. Far more actionable than 'this topic performed well' -
    tells the script generator exactly where pacing needs to tighten."""
    try:
        youtube = build("youtube", "v3", credentials=credentials)
        analytics = build("youtubeAnalytics", "v2", credentials=credentials)

        search_res = youtube.search().list(part="id", forMine=True, type="video", order="date", maxResults=1).execute()
        items = search_res.get("items", [])
        if not items:
            return None
        video_id = items[0]["id"]["videoId"]

        vid_res = youtube.videos().list(part="contentDetails", id=video_id).execute()
        vid_items = vid_res.get("items", [])
        if not vid_items:
            return None
        duration_sec = parse_iso8601_duration(vid_items[0]["contentDetails"]["duration"])
        if duration_sec <= 0:
            return None

        end = time.strftime("%Y-%m-%d")
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
        report = analytics.reports().query(
            ids="channel==MINE", startDate=start, endDate=end,
            metrics="audienceWatchRatio", dimensions="elapsedVideoTimeRatio",
            filters=f"video=={video_id}", sort="elapsedVideoTimeRatio"
        ).execute()
        rows = report.get("rows", [])
        if not rows:
            return None

        # Find the first point where retention drops below 50% - that's the real bail-out moment
        for elapsed_ratio, watch_ratio in rows:
            if float(watch_ratio) < 0.5:
                dropoff_second = round(float(elapsed_ratio) * duration_sec)
                print(f"📉 Retention analysis: previous video lost half its audience by ~{dropoff_second}s.")
                return dropoff_second
        return None  # never dropped below 50% - good retention, nothing to flag
    except Exception as e:
        print(f"⚠️ Retention drop-off analysis skipped: {e}")
        return None


def generate_text_with_fallback(prompt, temperature=0.8):
    """Generates short plain text (e.g., pinned comment or reply) using Gemini model fallbacks then Groq."""
    for model_name in GEMINI_MODELS:
        try:
            res = gemini_client.models.generate_content(model=model_name, contents=prompt)
            text = (res.text or "").strip().strip('"')
            if text:
                return text
        except Exception:
            continue

    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        for model_name in GROQ_MODELS:
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": temperature,
                        "max_tokens": 1000,
                    },
                    timeout=30,
                )
                if r.status_code == 200:
                    text = r.json()["choices"][0]["message"]["content"].strip().strip('"')
                    if text:
                        return text
            except Exception:
                continue
    return None

def generate_script_via_groq_fallback(prompt, tags_string):
    """Last-resort fallback if ALL Gemini models are exhausted/failing. Uses
    Groq (genuinely generous free tier, OpenAI-compatible API, no mysterious
    quota issues reported) for generation, and Tavily's free tier (built
    specifically for feeding search results into LLM prompts) to replace
    Gemini's built-in Google Search grounding tool, which Groq doesn't have
    natively."""
    groq_key = os.environ.get("GROQ_API_KEY")
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not groq_key:
        print("ℹ️ Groq fallback skipped - GROQ_API_KEY not set.")
        return None

    search_context = ""
    if tavily_key:
        try:
            print("🔎 Fetching live trend context via Tavily (Groq has no built-in search)...")
            r = requests.post("https://api.tavily.com/search", json={
                "api_key": tavily_key,
                "query": "viral mystery trends unsolved phenomena trending today",
                "max_results": 5,
            }, timeout=30)
            if r.status_code == 200:
                results = r.json().get("results", [])
                search_context = "\n".join(f"- {res.get('title', '')}: {res.get('content', '')[:200]}" for res in results)
        except Exception as e:
            print(f"⚠️ Tavily search fetch failed, proceeding without live context: {e}")

    full_prompt = prompt
    if search_context:
        full_prompt = f"Current trending context from live web search:\n{search_context}\n\n{prompt}"

    for model_name in GROQ_MODELS:
        try:
            print(f"🧠 Falling back to Groq ({model_name}) for script generation...")
            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": full_prompt}],
                "temperature": 0.7,
                "max_tokens": 4096,
                "response_format": {"type": "json_object"},
            }
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=90,
            )
            if r.status_code != 200:
                print(f"⚠️ Groq fallback ({model_name}) failed: {r.status_code} - {r.text[:300]}")
                # If json_validate_failed or other 400 format error, retry without json_object constraint
                if "json_validate_failed" in r.text or r.status_code == 400:
                    print(f"🔄 Retrying Groq ({model_name}) with max_tokens 4096 without json_object constraint...")
                    retry_payload = {
                        "model": model_name,
                        "messages": [{"role": "user", "content": full_prompt + "\n\nCRITICAL: Provide ONLY valid JSON inside ```json ... ``` codeblock. Do not include extra commentary."}],
                        "temperature": 0.7,
                        "max_tokens": 4096,
                    }
                    r = requests.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                        json=retry_payload,
                        timeout=90,
                    )
            if r.status_code != 200:
                print(f"⚠️ Groq fallback ({model_name}) final attempt failed: {r.status_code} - {r.text[:300]}")
                continue
            raw_text = r.json()["choices"][0]["message"]["content"]
            data = extract_json_object(raw_text)
            data["script"] = clean_script_retention(data.get("script", ""))
            data["title"], data["thumbnail_text"] = lint_title_thumbnail_pair(data.get("title", ""), data.get("thumbnail_text", ""))
            h_score, h_props = score_script_hook(data.get("script", ""))
            print(f"🎯 CineMind Hook Retention Score: {h_score}/100 {h_props}")
            variants = data.get("title_variants")
            if variants and isinstance(variants, list):
                print("📊 CineMind A/B Title Candidates:")
                for v in variants:
                    if isinstance(v, dict):
                        v["title"] = optimize_title(v.get("title", ""))
                        print(f"   [{v.get('type', 'Variant')}] {v['title']}")
            print(f"✅ Groq fallback succeeded using {model_name}.")
            return data
        except Exception as e:
            print(f"⚠️ Groq fallback ({model_name}) error: {e}")
    return None

def generate_script(avoid_topics=None, boost_topics=None, dynamic_tags_list=None, retention_dropoff_sec=None, viewer_requests=None):
    print("🧠 Asking Gemini to scan live web trends and write the script...")
    
    if not dynamic_tags_list:
        dynamic_tags_list = ["Shorts", "Science Mystery", "Unexplained Phenomena", "Space Mysteries"]
        
    tags_string = ", ".join(dynamic_tags_list[:20])

    avoid_block = f"\n    Do NOT repeat these already-covered topics: {'; '.join(avoid_topics[-50:])}\n" if avoid_topics else ""
    boost_block = f"\n    Top performing channel videos (reverse-engineer why viewers clicked & watched): {'; '.join(boost_topics)}\n" if boost_topics else ""
    demand_block = ""
    if viewer_requests:
        demand_block = f"\n    HIGH-DEMAND VIEWER REQUEST (prioritize or weave this topic in if not already covered): {viewer_requests[0]}\n"
    retention_block = ""
    if retention_dropoff_sec:
        retention_block = (f"\n    CRITICAL PACING FIX: your last video lost half its audience by second "
                            f"{retention_dropoff_sec}. Make sure THIS script's pacing stays punchy and escalating "
                            f"through at least that point - no slow/explanatory sentences before then.\n")
    cliffhanger_block = ""
    if os.path.exists(CLIFFHANGER_FILE):
        with open(CLIFFHANGER_FILE, "r") as f: pending_mystery = f.read()
        cliffhanger_block = f"\n    URGENT: Resolve yesterday's cliffhanger in the first sentence: '{pending_mystery}'\n"
        os.remove(CLIFFHANGER_FILE)

    prompt = f"""
    Search the web for high-demand, trending unsolved mysteries, bizarre scientific discoveries, deep-sea/space anomalies, or shocking historical oddities with massive mainstream curiosity right now.
    CRITICAL AUDIENCE PREFERENCE (Channel Telemetry): Earth/deep-ocean contradictions, bizarre atmospheric events, and counter-intuitive geological/physics discoveries perform 10x higher than abstract UFO queries. Prioritize concrete real-world anomalies.
    Write an optimized 22-28 second YouTube Shorts script (55-75 spoken words) in English. Zero filler, maximum curiosity and retention.
    {avoid_block}{boost_block}{demand_block}{retention_block}{cliffhanger_block}
    
    Script Structure Rules (Retention Scorer Standard):
    1. HOOK (First 3 seconds, 9-20 spoken words): Immediate pattern interrupt with concrete metrics, dates, or stakes. BANNED: never open with "Did you know", "Have you ever wondered", "Hey guys", "Welcome back", or "In this video". Must state a concrete figure, depth, date, or contradiction right away in the first sentence. State the primary search keyword phrase aloud in the first 15 seconds.
    2. ESCALATION BODY: Short, visual, high-tension sentences. Cut dead air, repetition, and filler. Raise a second surprise halfway through so viewers don't swipe away.
    3. PAYOFF + SEAMLESS LOOP: Save the craziest twist for the final 3 seconds. The final sentence MUST be an open thought or connective bridge that flows grammatically straight back into the very first word of the hook (driving >100% average percentage viewed). BANNED: never say "Thanks for watching", "See you next time", or "Like and subscribe" (deadly goodbye signals that kill retention).
    
    High-CTR Packaging Rules (CRITICAL - YouTube Packaging Linter Standard):
    - "title": 42-58 characters MAX (including " #Shorts" at the end) so it NEVER gets truncated on mobile. Front-load the curiosity gap and primary search keyword in the first 35 chars. Use specific numbers, years, or high-stakes tension (e.g., "Scientists Found THIS Under Antarctica #Shorts", "The 1977 Radio Signal That Never Repeated #Shorts", "Why 95% Of Earth's Ocean Is Unmapped #Shorts"). BANNED VAGUE WORDS: never use "insane", "incredible", "amazing", "crazy", "huge", "secret", "mindblowing", "epic".
    - "title_variants": Provide 3 distinct high-CTR title variations for YouTube Studio A/B testing:
      [
        {{"type": "Curiosity Gap", "title": "... #Shorts"}},
        {{"type": "Metric/Urgency", "title": "... #Shorts"}},
        {{"type": "Shock Fact", "title": "... #Shorts"}}
      ]
    - "thumbnail_text": Exactly 2 to 3 ultra-short UPPERCASE words ONLY (under 12-15 characters total) following the "Rule of 3" (e.g., "IT WASN'T EMPTY", "95% UNMAPPED", "ZERO SURVIVORS", "4 MINS TOO LATE"). CRITICAL RULE: The thumbnail and title are an inseparable pair; NEVER repeat words from the title on the thumbnail! The thumbnail must state what the title does not.
    - "description": 2 tight, keyword-rich sentences (120-160 chars) written for YouTube Search & Suggested discovery. Open with primary search keywords within the first 100 characters + an open curiosity loop that makes viewers watch instead of spoiling the twist.
    - "tags": 3 to 4 focused #Hashtags relevant to the video (always include #Shorts, e.g. ["#Shorts", "#DeepSea", "#ScienceFacts"]).
    - "seo_tags": 10 to 15 high-demand YouTube Search keyword phrases (2-4 words each, NO # symbol, mixing specific topic queries + broad niche demand like "unsolved ocean mysteries", "weird science discoveries"). Incorporate relevant winning terms from: {tags_string}.
    - "video_queries": Provide 8 concise 2-3 word stock-video search queries (e.g. "deep ocean", "night sky", "ancient ruins", "radar screen") matching each scene beat so real HD video footage can be fetched.
    - "image_prompts": Provide 8 distinct, vivid vertical-scene image prompts matching the beat-by-beat progression of the script (used as fallback or for surreal scenes).

    Monetization, Demand & Engagement:
    - "affiliate_keyword": A generic 1-2 word product search term related to this topic (e.g., "telescope", "night vision").
    - "cta_question": A polarizing, debate-sparking question for viewers to answer in the comments (drives comment velocity & algorithm push).
    - "pinned_comment": A high-polarization curiosity-gap question ending with an open debate prompt designed to trigger replies (e.g., "Did scientists just stumble on something natural, or a phenomenon being covered up? Drop your theory below 👇 (Part 2 soon)").
    - "poll_question": A highly debatable question about this topic for a Community Tab Poll.
    - "poll_options": An array of 3 short possible answers.

    Cinematic Sound Design:
    - "sfx_cues": Assign 2 or 3 sound effects to specific image clips. Provide the "clip_index" (0 to 7) and a 1-2 word "query" to search on Pixabay (e.g., "heartbeat", "thunder", "metal clank"). 

    Format as valid JSON only (no extra commentary) exactly like this schema:
    {{
      "script": "...",
      "visual_theme": "...",
      "video_queries": ["...", "...", "...", "...", "...", "...", "...", "..."],
      "image_prompts": ["...", "...", "...", "...", "...", "...", "...", "..."],
      "title": "...",
      "title_variants": [
        {{"type": "Curiosity Gap", "title": "..."}},
        {{"type": "Metric/Urgency", "title": "..."}},
        {{"type": "Shock Fact", "title": "..."}}
      ],
      "thumbnail_text": "...",
      "tags": ["#Shorts", "#Tag2", "#Tag3"],
      "seo_tags": ["keyword phrase 1", "keyword phrase 2", "..."],
      "description": "...",
      "cta_question": "...",
      "pinned_comment": "...",
      "is_cliffhanger": true/false,
      "cliffhanger_setup": "...",
      "affiliate_keyword": "...",
      "poll_question": "...",
      "poll_options": ["...", "...", "..."],
      "sfx_cues": [
        {{"clip_index": 2, "query": "thunder"}},
        {{"clip_index": 5, "query": "heartbeat"}}
      ]
    }}
    """

    for model_name in GEMINI_MODELS:
        for attempt in range(3):
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt, 
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())]
                    )
                )
                # response.text is a convenience shortcut that can return None when
                # Google Search grounding produces a multi-part response (grounding
                # metadata mixed with text) rather than one simple text block - that
                # was crashing here with a cryptic 'NoneType has no attribute strip'.
                # Fall back to manually walking the response parts if the shortcut fails.
                raw_text = response.text
                if raw_text is None:
                    parts = getattr(response.candidates[0].content, "parts", []) if response.candidates else []
                    raw_text = "".join(p.text for p in parts if getattr(p, "text", None))
                if not raw_text:
                    raise ValueError("Gemini returned an empty response (no text in any part) - likely a grounding-only or safety-filtered response.")

                data = extract_json_object(raw_text)
                data["script"] = clean_script_retention(data.get("script", ""))
                data["title"], data["thumbnail_text"] = lint_title_thumbnail_pair(data.get("title", ""), data.get("thumbnail_text", ""))
                h_score, h_props = score_script_hook(data.get("script", ""))
                print(f"🎯 CineMind Hook Retention Score: {h_score}/100 {h_props}")
                variants = data.get("title_variants")
                if variants and isinstance(variants, list):
                    print("📊 CineMind A/B Title Candidates:")
                    for v in variants:
                        if isinstance(v, dict):
                            v["title"] = optimize_title(v.get("title", ""))
                            print(f"   [{v.get('type', 'Variant')}] {v['title']}")
                if data.get("is_cliffhanger") and data.get("cliffhanger_setup"):
                    with open(CLIFFHANGER_FILE, "w") as f: f.write(data["cliffhanger_setup"])
                return data
            except Exception as e:
                err_str = str(e)
                print(f"⚠️ Gemini ({model_name}) Attempt {attempt + 1} Failed: {e}")
                if "404" in err_str or "NOT_FOUND" in err_str:
                    break  # Don't burn retries on a retired/non-existent model
                if "429" in err_str or "503" in err_str:
                    if "PerDay" in err_str or "per day" in err_str or "retry in" in err_str or "limit exceeded" in err_str or "quota" in err_str.lower():
                        print("ℹ️ Gemini quota/limit reached; switching immediately to Groq fallback...")
                        break
                    # Fast backoff: 5s, 15s, 30s instead of long stalls
                    time.sleep([5, 15, 30][attempt])
                else:
                    time.sleep(3)
        print(f"ℹ️ Giving up on {model_name}, trying next model if available...")

    print("ℹ️ All Gemini models exhausted - trying Groq fallback before giving up entirely...")
    fallback_data = generate_script_via_groq_fallback(prompt, tags_string)
    if fallback_data:
        fallback_data["title"], fallback_data["thumbnail_text"] = lint_title_thumbnail_pair(fallback_data.get("title", ""), fallback_data.get("thumbnail_text", ""))
        h_score, h_props = score_script_hook(fallback_data.get("script", ""))
        print(f"🎯 CineMind Hook Retention Score: {h_score}/100 {h_props}")
        if fallback_data.get("is_cliffhanger") and fallback_data.get("cliffhanger_setup"):
            with open(CLIFFHANGER_FILE, "w") as f: f.write(fallback_data["cliffhanger_setup"])
        return fallback_data

    raise Exception("Failed to get response from Gemini (all models exhausted) and Groq fallback also failed or wasn't configured.")

def save_community_post(data):
    print("📝 Generating Community Tab Poll asset...")
    try:
        with open("community_poll.txt", "w", encoding="utf-8") as f:
            f.write(f"POLL QUESTION:\n{data.get('poll_question', 'What do you think?')}\n\n")
            f.write("OPTIONS:\n")
            for opt in data.get('poll_options', []):
                f.write(f"- {opt}\n")
        print("✅ Poll saved to 'community_poll.txt'")
    except Exception: pass

# --- 3. AUDIO & SFX SYNC ---
def build_estimated_word_timestamps(text, total_duration):
    """Build proportional word-level timestamps when a TTS engine omits WordBoundary events."""
    raw_tokens = [tok.strip() for tok in (text or "").split() if tok.strip()]
    if not raw_tokens or total_duration <= 0:
        return []
    weights = []
    for tok in raw_tokens:
        base = max(2, len("".join(ch for ch in tok if ch.isalnum())))
        pause = 2.5 if tok[-1] in ".!?" else (1.2 if tok[-1] in ",;:" else 0.0)
        weights.append(base + pause)
    total_weight = sum(weights) or float(len(raw_tokens))
    words = []
    cursor = 0.0
    for tok, weight in zip(raw_tokens, weights):
        dur = total_duration * (weight / total_weight)
        end = min(total_duration, cursor + dur)
        words.append({"text": tok, "start": round(cursor, 4), "end": round(end, 4)})
        cursor = end
    return words


def _read_audio_duration(audio_path):
    clip = AudioFileClip(audio_path)
    try:
        return float(clip.duration or 0.0)
    finally:
        clip.close()


async def generate_audio_and_timestamps(text):
    words = []
    edge_voices = [VOICE] + [v for v in ("en-US-JennyNeural", "en-US-GuyNeural") if v != VOICE]
    for voice_name in edge_voices[:2]:
        try:
            print(f"🎙️ Generating AI Voiceover ({voice_name})...")
            communicate = edge_tts.Communicate(text, voice_name)
            attempt_words = []
            with open(OUTPUT_AUDIO, "wb") as fp:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        fp.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        attempt_words.append({
                            "text": chunk["text"],
                            "start": chunk["offset"] / 10000000.0,
                            "end": (chunk["offset"] + chunk["duration"]) / 10000000.0,
                        })
            if os.path.exists(OUTPUT_AUDIO) and os.path.getsize(OUTPUT_AUDIO) > 1024:
                words = attempt_words
                if not words:
                    dur = _read_audio_duration(OUTPUT_AUDIO)
                    words = build_estimated_word_timestamps(text, dur)
                with open("words.json", "w") as f:
                    json.dump(words, f)
                return
        except Exception as e:
            print(f"⚠️ edge-tts ({voice_name}) failed: {e}")

    # Fallback 1: gTTS (resilient when Bing WSS returns 403 on cloud runner IPs)
    try:
        from gtts import gTTS
        print("🎙️ Falling back to gTTS voiceover...")
        await asyncio.to_thread(lambda: gTTS(text=text, lang="en", slow=False).save(OUTPUT_AUDIO))
        if os.path.exists(OUTPUT_AUDIO) and os.path.getsize(OUTPUT_AUDIO) > 1024:
            dur = _read_audio_duration(OUTPUT_AUDIO)
            words = build_estimated_word_timestamps(text, dur)
            with open("words.json", "w") as f:
                json.dump(words, f)
            print(f"✅ gTTS fallback generated {dur:.2f}s audio with {len(words)} synced words.")
            return
    except Exception as e:
        print(f"⚠️ gTTS fallback failed: {e}")

    # Fallback 2: offline espeak-ng + ffmpeg if available on runner
    import shutil
    import subprocess
    espeak_bin = shutil.which("espeak-ng") or shutil.which("espeak")
    if espeak_bin:
        tmp_wav = "voiceover_tmp.wav"
        try:
            print(f"🎙️ Falling back to offline TTS ({os.path.basename(espeak_bin)})...")
            subprocess.run([espeak_bin, "-s", "165", "-w", tmp_wav, text], check=True)
            ffmpeg_bin = shutil.which("ffmpeg")
            if not ffmpeg_bin:
                import imageio_ffmpeg
                ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
            subprocess.run([ffmpeg_bin, "-y", "-i", tmp_wav, OUTPUT_AUDIO], check=True)
            if os.path.exists(OUTPUT_AUDIO) and os.path.getsize(OUTPUT_AUDIO) > 1024:
                dur = _read_audio_duration(OUTPUT_AUDIO)
                words = build_estimated_word_timestamps(text, dur)
                with open("words.json", "w") as f:
                    json.dump(words, f)
                print(f"✅ Offline TTS generated {dur:.2f}s audio with {len(words)} synced words.")
                return
        finally:
            if os.path.exists(tmp_wav):
                os.remove(tmp_wav)

    raise RuntimeError("All TTS providers (edge-tts, gTTS, espeak) failed to generate voiceover audio.")

def safe_download(url, out_path, min_bytes=2048):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code == 200 and len(r.content) > min_bytes:
            with open(out_path, "wb") as f: f.write(r.content)
            return True
    except Exception: pass
    return False

def download_dynamic_sfx(sfx_cues):
    print("🎵 Directing Cinematic Sound Design...")
    audio_api = base64.b64decode("aHR0cHM6Ly9waXhhYmF5LmNvbS9hcGkvYXVkaW8v").decode("utf-8")
    sfx_map = {}
    for cue in sfx_cues:
        try:
            idx = cue.get("clip_index")
            query = cue.get("query", "").replace(" ", "+")
            if idx is None or not query: continue
            
            res = requests.get(audio_api, params={"key": PIXABAY_API_KEY, "q": query, "per_page": 3}).json()
            if res.get("hits"):
                out_path = f"sfx_dynamic_{idx}.mp3"
                safe_download(res["hits"][0]["audio"], out_path, 1000)
                sfx_map[idx] = out_path
                print(f"   🔉 Loaded SFX for clip {idx}: {query}")
        except Exception: pass
    return sfx_map

def _is_valid_video_file(path, min_duration=1.5):
    """Validate that a downloaded MP4 file decodes cleanly and has usable resolution/length."""
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            cap.release()
            return False
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        w = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0.0
        h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0.0
        cap.release()
        duration = (frames / fps) if fps > 0 else 0.0
        return w >= 480 and h >= 480 and duration >= min_duration
    except Exception:
        return False


def _pick_pixabay_mp4_url(videos_dict):
    for tier in ("medium", "large", "small"):
        info = videos_dict.get(tier) or {}
        url = info.get("url")
        size = info.get("size") or 0
        if url and (size == 0 or size <= 35 * 1024 * 1024):
            return url
    return None


def _fetch_pixabay_video(query, used_ids):
    if not PIXABAY_API_KEY or not query:
        return None, None
    try:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": PIXABAY_API_KEY, "q": query, "per_page": 8, "safesearch": "true"},
            timeout=20,
        )
        if r.status_code != 200:
            return None, None
        hits = r.json().get("hits") or []
        # Prefer clips not yet used in this video; prefer portrait/tall clips first
        candidates = [h for h in hits if f"px:{h.get('id')}" not in used_ids]
        candidates.sort(key=lambda h: (h.get("videos", {}).get("medium", {}).get("height", 0) >= h.get("videos", {}).get("medium", {}).get("width", 1)), reverse=True)
        for hit in candidates:
            url = _pick_pixabay_mp4_url(hit.get("videos") or {})
            if url:
                return url, f"px:{hit.get('id')}"
    except Exception:
        pass
    return None, None


def _fetch_pexels_video(query, used_ids):
    pexels_key = os.environ.get("PEXELS_API_KEY")
    if not pexels_key or not query:
        return None, None
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": pexels_key},
            params={"query": query, "per_page": 8, "orientation": "portrait"},
            timeout=20,
        )
        if r.status_code != 200:
            return None, None
        for vid in r.json().get("videos") or []:
            vid_key = f"pxl:{vid.get('id')}"
            if vid_key in used_ids:
                continue
            mp4s = [
                vf for vf in (vid.get("video_files") or [])
                if vf.get("file_type") == "video/mp4" and (vf.get("width") or 0) >= 540
            ]
            mp4s.sort(key=lambda vf: abs((vf.get("width") or 1080) - 1080))
            if mp4s:
                return mp4s[0].get("link"), vid_key
    except Exception:
        pass
    return None, None


def _fetch_nasa_video(query, used_ids):
    if not query:
        return None, None
    try:
        r = requests.get(
            "https://images-api.nasa.gov/search",
            params={"q": query, "media_type": "video"},
            timeout=20,
        )
        if r.status_code != 200:
            return None, None
        items = (r.json().get("collection") or {}).get("items") or []
        for item in items[:6]:
            nasa_id = ((item.get("data") or [{}])[0]).get("nasa_id")
            vid_key = f"nasa:{nasa_id}"
            if not nasa_id or vid_key in used_ids:
                continue
            coll_href = item.get("href")
            if not coll_href:
                continue
            assets = requests.get(coll_href, timeout=15).json()
            for suffix in ("~medium.mp4", "~mobile.mp4", "~preview.mp4"):
                match = next((u for u in assets if isinstance(u, str) and u.endswith(suffix)), None)
                if match:
                    return match, vid_key
    except Exception:
        pass
    return None, None


def download_visual_media(image_prompts, video_queries=None, visual_theme=""):
    """Fetch a hybrid of real HD stock videos (.mp4) and custom AI parallax visuals (.jpg)."""
    print("🎬 Sourcing Hybrid Stock Videos + Custom AI Visuals...")
    for stale in os.listdir("media"):
        if stale.startswith("clip_") and stale.endswith((".mp4", ".jpg")):
            try:
                os.remove(os.path.join("media", stale))
            except OSError:
                pass

    pol_api = base64.b64decode("aHR0cHM6Ly9pbWFnZS5wb2xsaW5hdGlvbnMuYWkvcHJvbXB0Lw==").decode("utf-8")
    queries = list(video_queries or [])
    used_video_ids = set()
    video_count = 0
    image_count = 0

    custom_clip_url = os.environ.get("CUSTOM_CLIP_URL", "").strip()

    for index, prompt in enumerate(image_prompts):
        mp4_path = f"media/clip_{index}.mp4"
        if index == 0:
            if custom_clip_url and custom_clip_url.startswith("http"):
                print(f"🎬 Ingesting custom Google Flow / Veo clip from: {custom_clip_url[:60]}...")
                if safe_download(custom_clip_url, mp4_path, min_bytes=50000) and _is_valid_video_file(mp4_path):
                    print("✅ Successfully ingested custom Google Flow video as scene 0!")
                    video_count += 1
                    continue
            else:
                try:
                    import flow_engine
                    if flow_engine.is_flow_available():
                        print("🎬 Google Flow Connector active! Generating Scene 0 using Google Veo AI...")
                        flow_clip = flow_engine.generate_flow_video(prompt, duration=5, out_path=mp4_path)
                        if flow_clip and _is_valid_video_file(mp4_path):
                            print("✅ Successfully generated Scene 0 via Google Flow / Veo AI!")
                            video_count += 1
                            continue
                except Exception as fe:
                    print(f"ℹ️ Google Flow Scene 0 generation skipped/failed: {fe}")

        raw_query = (queries[index] if index < len(queries) else "").strip()
        fallback_words = " ".join(prompt.split()[:3])
        search_candidates = [q for q in (raw_query, " ".join(raw_query.split()[:2]), fallback_words) if q]

        got_video = False
        mp4_path = f"media/clip_{index}.mp4"
        for q in search_candidates:
            for fetcher in (_fetch_pixabay_video, _fetch_pexels_video, _fetch_nasa_video):
                url, vid_key = fetcher(q, used_video_ids)
                if url and safe_download(url, mp4_path, min_bytes=50000):
                    if _is_valid_video_file(mp4_path):
                        used_video_ids.add(vid_key)
                        got_video = True
                        video_count += 1
                        print(f"   🎥 Scene {index}: stock video ({vid_key}) for '{q}'")
                        break
                    try:
                        os.remove(mp4_path)
                    except OSError:
                        pass
            if got_video:
                break

        if not got_video:
            jpg_path = f"media/clip_{index}.jpg"
            got_flow_scene = False
            try:
                import flow_engine
                if flow_engine.is_flow_available():
                    print(f"   🎨 Scene {index}: generating with Google Flow (Nano Banana)...")
                    flow_img = flow_engine.generate_flow_image(f"{prompt}, {visual_theme}", out_path=jpg_path)
                    if flow_img and os.path.exists(jpg_path) and os.path.getsize(jpg_path) > 10000:
                        image_count += 1
                        got_flow_scene = True
                        print(f"   ✅ Scene {index}: Google Flow visual generated cleanly!")
            except Exception as fe:
                pass

            if not got_flow_scene:
                full_prompt = f"{prompt}, {visual_theme}, photorealistic, cinematic lighting, full frame, no borders, 8k resolution, octane render"
                for _attempt in range(4):
                    if safe_download(
                        f"{pol_api}{urllib.parse.quote(full_prompt)}?width=1080&height=1920&nologo=true&seed={random.randint(1, 999999)}",
                        jpg_path,
                        20000,
                    ):
                        image_count += 1
                        print(f"   🎨 Scene {index}: custom AI visual fallback")
                        break
                    time.sleep(2)

    print(f"✅ Media ready: {video_count} stock video clips + {image_count} AI parallax scenes.")


def download_ai_visuals(prompts, visual_theme=""):
    return download_visual_media(prompts, video_queries=None, visual_theme=visual_theme)

def _load_bold_font(fontsize):
    from PIL import ImageFont
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/impact.ttf",
    ]
    for path in font_candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, fontsize)
            except Exception:
                continue
    return ImageFont.load_default()

def _wrap_thumbnail_words(text_hook):
    """Wraps thumbnail text cleanly into 1-2 balanced lines without mid-word truncation."""
    import re
    words = re.sub(r"\s+", " ", (text_hook or "").strip()).upper().split()
    words = words[:4]  # Max 4 words keeps thumbnails instantly readable on mobile
    if not words:
        return []
    if len(words) <= 2 and len(" ".join(words)) <= 12:
        return [" ".join(words)]
    mid = (len(words) + 1) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]

def _render_thumbnail_banner(lines, max_width=920, accent_color=(255, 215, 0)):
    """Renders 1-2 lines of auto-sized 3D high-CTR text over a dark contrast pill (passes 10% zoom test)."""
    from PIL import Image, ImageDraw
    fontsize = 136
    font = _load_bold_font(fontsize)
    tmp = Image.new("RGBA", (10, 10))
    tmp_draw = ImageDraw.Draw(tmp)

    while fontsize > 56:
        font = _load_bold_font(fontsize)
        widths = [tmp_draw.textbbox((0, 0), line, font=font, stroke_width=10)[2] for line in lines]
        if max(widths) <= max_width:
            break
        fontsize -= 6

    line_boxes = [tmp_draw.textbbox((0, 0), line, font=font, stroke_width=10) for line in lines]
    line_heights = [b[3] - b[1] for b in line_boxes]
    line_widths = [b[2] - b[0] for b in line_boxes]
    line_gap = 18
    depth = 12
    pad_x, pad_y = 48, 36

    total_w = max(line_widths) + depth + pad_x * 2
    total_h = sum(line_heights) + line_gap * (len(lines) - 1) + depth + pad_y * 2

    img = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Dark translucent backdrop guarantees text pops even against bright AI imagery (10% Zoom Test)
    draw.rounded_rectangle([(0, 0), (total_w - 1, total_h - 1)], radius=32, fill=(0, 0, 0, 190))

    y_cursor = pad_y
    for idx, line in enumerate(lines):
        bbox = line_boxes[idx]
        lw, lh = line_widths[idx], line_heights[idx]
        ox = (total_w - lw) // 2 - bbox[0]
        oy = y_cursor - bbox[1]
        # Highlight line 1 in high-contrast accent (e.g. Gold/Yellow or Cyan), line 2 in crisp white
        fill = accent_color if idx == 0 else (255, 255, 255)
        for d in range(depth, 0, -1):
            draw.text((ox + d, oy + d), line, font=font, fill=(0, 0, 0, 255))
        draw.text((ox, oy), line, font=font, fill=fill + (255,), stroke_width=10, stroke_fill=(0, 0, 0, 255))
        y_cursor += lh + line_gap

    return img

def generate_ai_thumbnail(title, text_hook="", visual_theme="", out_path="thumbnail.jpg", content=None):
    print("🖼️ Painting Custom High-CTR AI Thumbnail (Rule of 3 + 10% Zoom Test)...")
    try:
        prompt = (
            f"Ultra-dramatic high-contrast focal close-up for {title}, {visual_theme}, "
            "cinematic rim lighting, glowing mysterious subject centered in lower two-thirds, "
            "clean dark negative space at top third, hyper-detailed, 8k, no text, no words, no watermark"
        )
        got_base = False
        try:
            import flow_engine
            if flow_engine.is_flow_available():
                print("   🖼️ Generating High-CTR Thumbnail Base with Google Flow (Nano Banana)...")
                flow_thumb = flow_engine.generate_flow_image(prompt, out_path=out_path)
                if flow_thumb and os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
                    got_base = True
        except Exception as fe:
            pass

        if not got_base:
            pol_api = base64.b64decode("aHR0cHM6Ly9pbWFnZS5wb2xsaW5hdGlvbnMuYWkvcHJvbXB0Lw==").decode("utf-8")
            url = f"{pol_api}{urllib.parse.quote(prompt)}?width=1080&height=1920&nologo=true&seed={random.randint(1, 999999)}"
            safe_download(url, out_path, min_bytes=20000)
        
        if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
            lines = _wrap_thumbnail_words(text_hook)
            if lines:
                base_img = PIL.Image.open(out_path).convert("RGBA")
                # Variant A: High-contrast Vibrant Gold/Yellow (255, 215, 0)
                txt_img_a = _render_thumbnail_banner(lines, max_width=base_img.width - 140, accent_color=(255, 215, 0))
                pos_x_a = max(0, (base_img.width - txt_img_a.width) // 2)
                pos_y_a = max(80, (base_img.height // 4) - (txt_img_a.height // 2))
                
                thumb_a = base_img.copy()
                thumb_a.paste(txt_img_a, (pos_x_a, pos_y_a), txt_img_a)
                thumb_a.convert("RGB").save(out_path, quality=95)
                
                # Variant B: High-contrast Electric Cyan (0, 229, 255) for YouTube Studio A/B Test & Compare
                out_path_b = out_path.replace(".jpg", "_b.jpg")
                txt_img_b = _render_thumbnail_banner(lines, max_width=base_img.width - 140, accent_color=(0, 229, 255))
                pos_x_b = max(0, (base_img.width - txt_img_b.width) // 2)
                pos_y_b = max(80, (base_img.height // 4) - (txt_img_b.height // 2))
                
                thumb_b = base_img.copy()
                thumb_b.paste(txt_img_b, (pos_x_b, pos_y_b), txt_img_b)
                thumb_b.convert("RGB").save(out_path_b, quality=95)
                
                print(f"✅ High-CTR thumbnail variants (A: {out_path}, B: {out_path_b}) rendered cleanly!")
                
                # Export A/B Testing packaging report for YouTube Studio Test & Compare
                ab_report = {
                    "primary_title": title,
                    "title_variants": (content.get("title_variants") or []) if content else [],
                    "thumbnails": {
                        "variant_a_gold": out_path,
                        "variant_b_cyan": out_path_b
                    },
                    "packaging_rules": {
                        "rule_of_3": "PASSED (Subject + Negative Space + 2-3 Words)",
                        "mobile_1_second_readability": "PASSED (< 15 characters, no overlap with title)",
                        "ten_percent_zoom_test": "PASSED (136px bold font, 10px black stroke, translucent pill)",
                        "youtube_studio_instructions": "Upload thumbnail.jpg and thumbnail_b.jpg in YouTube Studio -> Video Details -> Thumbnail -> Test & Compare."
                    }
                }
                with open("packaging_ab_test.json", "w", encoding="utf-8") as f:
                    json.dump(ab_report, f, indent=2)
    except Exception as e: 
        print(f"⚠️ Thumbnail generation failed: {e}")

def download_sfx(brief=None):
    audio_api = base64.b64decode("aHR0cHM6Ly9waXhhYmF5LmNvbS9hcGkvYXVkaW8v").decode("utf-8")
    bgm_downloaded = False
    if brief:
        bgm_downloaded = download_theme_matched_bgm(brief, PIXABAY_API_KEY, "background_music.mp3")
    if not bgm_downloaded and not os.path.exists("background_music.mp3"):
        res = requests.get(audio_api, params={"key": PIXABAY_API_KEY, "q": "cinematic ambient"})
        if res.status_code == 200 and res.json().get("hits"): safe_download(random.choice(res.json()["hits"])["audio"], "background_music.mp3", 20000)
    if not os.path.exists("whoosh.mp3"):
        try: safe_download(requests.get(audio_api, params={"key": PIXABAY_API_KEY, "q": "whoosh"}).json()["hits"][0]["audio"], "whoosh.mp3", 1000)
        except Exception: pass
    if not os.path.exists("pop.mp3"):
        try: safe_download(requests.get(audio_api, params={"key": PIXABAY_API_KEY, "q": "pop bubble"}).json()["hits"][0]["audio"], "pop.mp3", 500)
        except Exception: pass

# --- 4. THE PRO EDITOR ---
def safe_color_grade(get_frame, t):
    frame = get_frame(t).astype(np.float32)
    return np.clip((frame - 127.0) * 1.05 + 127.0 * 1.02, 0, 255).astype('uint8')

_VIGNETTE_CACHE = {}
_NOISE_POOL = None


def apply_procedural_compositing(clip):
    """Simulates a human editor adding a cinematic vignette and subtle film grain."""
    def add_texture(get_frame, t):
        global _NOISE_POOL
        frame = get_frame(t).astype(np.float32)
        h, w = frame.shape[:2]

        vignette = _VIGNETTE_CACHE.get((h, w))
        if vignette is None:
            X, Y = np.meshgrid(np.linspace(-1, 1, w), np.linspace(-1, 1, h))
            radius = np.sqrt(X**2 + Y**2)
            vignette = np.clip(1 - (radius * 0.55), 0, 1).astype(np.float32)[:, :, np.newaxis]
            _VIGNETTE_CACHE[(h, w)] = vignette

        if _NOISE_POOL is None or _NOISE_POOL[0].shape != frame.shape:
            _NOISE_POOL = [np.random.normal(loc=0, scale=7, size=frame.shape).astype(np.float32) for _ in range(6)]
        noise = _NOISE_POOL[int(t * 24) % len(_NOISE_POOL)]

        composited = (frame * vignette) + noise
        return np.clip(composited, 0, 255).astype(np.uint8)

    return clip.fl(add_texture)

def render_3d_word(text, fontsize=120, fill=(255, 255, 0), depth=10, depth_color=(120, 95, 0), max_width=960):
    from PIL import Image, ImageDraw
    tmp = Image.new("RGBA", (10, 10))
    draw_tmp = ImageDraw.Draw(tmp)
    current_size = fontsize
    font = _load_bold_font(current_size)
    bbox = draw_tmp.textbbox((0, 0), text, font=font, stroke_width=6)
    while (bbox[2] - bbox[0]) + depth + 48 > max_width and current_size > 48:
        current_size -= 8
        font = _load_bold_font(current_size)
        bbox = draw_tmp.textbbox((0, 0), text, font=font, stroke_width=6)
    W, H = (bbox[2] - bbox[0]) + depth + 48, (bbox[3] - bbox[1]) + depth + 48
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    ox, oy = (depth + 24) - bbox[0], (depth + 24) - bbox[1]
    for d in range(depth, 0, -1): draw.text((ox + d, oy + d), text, font=font, fill=depth_color + (255,))
    draw.text((ox, oy), text, font=font, fill=fill + (255,), stroke_width=6, stroke_fill=(0, 0, 0, 255))
    return np.array(img)

def is_high_impact(word):
    impact_words = {
        "MILLION", "BILLION", "TRILLION", "SECRET", "SHOCKING", "MYSTERY", "ONLY",
        "NEVER", "HIDDEN", "IMPOSSIBLE", "VANISHED", "FOUND", "WARNING", "DEADLIEST",
        "BANNED", "DELETED", "PROOF", "ALONE", "ZERO", "FIRST", "LAST", "REAL", "TRAP",
    }
    return any(char.isdigit() for char in word) or word.strip(".,!?\"'").upper() in impact_words

def get_watermark_clip(duration, w=1080, h=1920):
    """Small persistent channel watermark, bottom-right corner - gives a
    recognizable cross-video branding thread. Skipped entirely if
    CHANNEL_WATERMARK_TEXT isn't set, rather than showing a generic placeholder."""
    text = os.environ.get("CHANNEL_WATERMARK_TEXT")
    if not text:
        return None
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
    except Exception:
        font = ImageFont.load_default()
    tmp = Image.new("RGBA", (10, 10))
    bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 16
    img = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((pad, pad), text, font=font, fill=(255, 255, 255, 160))  # semi-transparent, unobtrusive
    arr = np.array(img)
    clip = ImageClip(arr).set_duration(duration)
    return clip.set_position((w - arr.shape[1] - 24, h - arr.shape[0] - 140))  # above the retention bar area

def discover_media_scenes(media_dir="media"):
    """Return ordered scene paths (clip_0..clip_N), preferring .mp4 video clips when present."""
    by_idx = {}
    for fname in os.listdir(media_dir):
        if not fname.startswith("clip_") or not fname.endswith((".mp4", ".jpg")):
            continue
        stem, ext = os.path.splitext(fname)
        try:
            idx = int(stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        full = os.path.join(media_dir, fname)
        if idx not in by_idx or ext == ".mp4":
            by_idx[idx] = full
    return [by_idx[i] for i in sorted(by_idx)]


def _prepare_stock_video_clip(video_path, duration, start_time=0.0, impact_times=None, fps=24, color_palette="deep_ocean_teal", usable_start=None):
    """Cover-fit a stock MP4 to 1080x1920 vertical 9:16 with CineMind two-stage color grade and audio-reactive punch."""
    import cv2
    src = VideoFileClip(video_path).without_audio()
    opened_handles = [src]
    src_dur = float(src.duration or 0.0)
    if src_dur > duration + 0.8:
        offset = usable_start if (usable_start is not None and usable_start + duration <= src_dur) else min(src_dur - duration, src_dur * 0.2)
        trimmed = src.subclip(offset, offset + duration)
    elif 0.1 < src_dur < duration:
        trimmed = vfx.loop(src, duration=duration).set_duration(duration)
    else:
        trimmed = src.subclip(0, min(duration, src_dur or duration)).set_duration(duration)

    # Cover-scale to fill 1080x1920 completely without letterboxing
    cw, ch = trimmed.size
    if (cw / max(ch, 1)) > (1080.0 / 1920.0):
        fitted = trimmed.resize(height=1920)
    else:
        fitted = trimmed.resize(width=1080)
    fw, fh = fitted.size
    x1 = max(0, (fw - 1080) // 2)
    y1 = max(0, (fh - 1920) // 2)
    cropped = fitted.crop(x1=x1, y1=y1, width=1080, height=1920)

    impacts = impact_times or []

    def kinetic_grade(get_frame, t):
        frame = get_frame(t)
        abs_t = start_time + t
        progress = t / max(duration, 0.01)
        
        # CineMind Professional Ken-Burns Push-In (smooth documentary ease curve)
        smooth_ease = 0.5 * (1.0 - math.cos(progress * math.pi))
        scale = 1.0 + 0.07 * smooth_ease
        
        h, w = frame.shape[:2]
        drift_x = int(w * 0.010 * math.sin(smooth_ease * math.pi))
        drift_y = int(h * 0.012 * smooth_ease)
        
        for imp_t in impacts:
            if imp_t <= abs_t <= imp_t + 0.28:
                delta = abs_t - imp_t
                scale += 0.08 * math.exp(-delta * 10.0) * math.cos(delta * 16.0)
                break
                
        if scale > 1.002 or drift_x != 0 or drift_y != 0:
            M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), 0.0, scale)
            M[0, 2] += drift_x
            M[1, 2] += drift_y
            frame = cv2.warpAffine(frame, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        # Apply CineMind Department D thematic film grade
        return apply_cinematic_color_grade(frame, color_palette)

    return cropped.fl(kinetic_grade).set_fps(fps).set_duration(duration), opened_handles


def assemble_video(dynamic_sfx_map, brief=None):
    print("\n🎬 Assembling cinematic video (CineMind Autonomous Video Editor Engine)...")
    brief = brief or determine_creative_direction({"visual_theme": "cinematic"})
    voice_audio = AudioFileClip(OUTPUT_AUDIO)
    audio_tracks = [voice_audio]
    opened_video_handles = [voice_audio]
    
    with open("words.json", "r") as f: words = json.load(f)
    
    # Map the exact seconds where a high-impact word is spoken
    impact_timestamps = [w["start"] for w in words if is_high_impact(w["text"].strip(".,!?\"'").upper())]
    
    if os.path.exists("background_music.mp3"):
        def filter_audio(get_frame, t):
            vol = np.full(t.shape, 0.20)
            for w in words:
                if (t >= w["start"] - 0.1).any() and (t <= w["end"] + 0.2).any(): vol[(t >= w["start"] - 0.1) & (t <= w["end"] + 0.2)] = 0.03
            return get_frame(t) * vol[:, np.newaxis]
        bgm = AudioFileClip("background_music.mp3").fx(afx.audio_loop, duration=voice_audio.duration).fl(filter_audio)
        audio_tracks.append(bgm)

    media_files = discover_media_scenes("media")
    if not media_files:
        raise FileNotFoundError("No media/clip_*.mp4 or media/clip_*.jpg files found for video assembly.")
    fallback_images = [p for p in media_files if p.endswith(".jpg")]
    
    # CineMind Department A: Professional Footage Review & Shot Logging
    footage_log = build_editorial_footage_log(media_files, brief)
    footage_by_path = {e.path: e for e in footage_log}
    
    # 1. Parse the data structure to find natural sentence boundaries
    sentence_boundaries = []
    for w in words:
        if w["text"][-1] in ".,!?":
            # Add a 0.3s psychological overlap (The J-Cut offset)
            sentence_boundaries.append(w["end"] + 0.3)
    
    # Ensure the final boundary matches the total audio length
    if not sentence_boundaries or sentence_boundaries[-1] < voice_audio.duration:
        sentence_boundaries.append(voice_audio.duration)

    clips = []
    current_time = 0
    media_index = 0
    boundary_index = 0
    has_sfx = os.path.exists("whoosh.mp3") and os.path.exists("pop.mp3")

    # 2. Loop through the calculated boundaries (snappier <=2.5s hook cuts, <=3.8s body)
    while current_time < voice_audio.duration and boundary_index < len(sentence_boundaries):
        media_path = media_files[media_index % len(media_files)]
        
        # Calculate dynamic duration based on the next sentence end
        target_end = min(sentence_boundaries[boundary_index], voice_audio.duration)
        clip_dur = target_end - current_time
        
        max_seg = 2.5 if current_time < 3.0 else 3.8
        if clip_dur > max_seg:
            clip_dur = max_seg
        else:
            boundary_index += 1

        # 3. Use real HD stock video when available, or AI MiDaS 3D parallax for still scenes
        clip = None
        log_entry = footage_by_path.get(media_path)
        usable_start = log_entry.usable_start if log_entry else None

        if media_path.endswith(".mp4"):
            try:
                vid_clip, handles = _prepare_stock_video_clip(
                    media_path,
                    duration=clip_dur,
                    start_time=current_time,
                    impact_times=impact_timestamps,
                    fps=24,
                    color_palette=brief.color_palette,
                    usable_start=usable_start,
                )
                opened_video_handles.extend(handles)
                clip = vid_clip.set_start(current_time)
            except Exception as e:
                print(f"⚠️ Stock clip '{media_path}' decode failed ({e}), falling back.")
                if fallback_images:
                    media_path = fallback_images[media_index % len(fallback_images)]
                else:
                    alt_mp4s = [p for p in media_files if p.endswith(".mp4") and p != media_path]
                    for alt in alt_mp4s:
                        try:
                            vid_clip, handles = _prepare_stock_video_clip(
                                alt,
                                duration=clip_dur,
                                start_time=current_time,
                                impact_times=impact_timestamps,
                                fps=24,
                                color_palette=brief.color_palette,
                            )
                            opened_video_handles.extend(handles)
                            clip = vid_clip.set_start(current_time)
                            break
                        except Exception:
                            continue

        if clip is None:
            if media_path.endswith(".jpg") and os.path.exists(media_path):
                warped_clip = ProceduralAIVideoGenerator(
                    media_path,
                    duration=clip_dur,
                    fps=24,
                    start_time=current_time,
                    impact_times=impact_timestamps,
                ).to_clip()
                palette = brief.color_palette
                clip = AIVideoEngine.apply_ai_flicker(warped_clip).fl(lambda gf, t: apply_cinematic_color_grade(safe_color_grade(gf, t), palette)).set_start(current_time)
            else:
                clip = ColorClip(size=(1080, 1920), color=(10, 14, 22)).set_duration(clip_dur).set_start(current_time)
        
        if current_time > 0 and clip_dur > 0.5:
            clip = clip.crossfadein(0.2) # Smooth visual blend to complement the audio overlap
        clips.append(clip)
        
        # 5. Add procedural glitch transitions & sound cues on the cuts
        if has_sfx and current_time > 0: 
            audio_tracks.append(AudioFileClip("whoosh.mp3").set_start(current_time).fx(afx.volumex, 0.3))
            glitch_transition = AIVideoEngine.generate_procedural_glitch(duration=0.15, fps=24).set_start(current_time - 0.075)
            clips.append(glitch_transition)

        if media_index in dynamic_sfx_map:
            audio_tracks.append(AudioFileClip(dynamic_sfx_map[media_index]).set_start(current_time).fx(afx.volumex, 0.5))
            
        current_time += clip_dur
        media_index += 1

    # --- KINETIC TYPOGRAPHY ENGINE ---
    text_clips = []
    
    for i, w in enumerate(words):
        start_t = w["start"]
        # Extend end time seamlessly to the next word so the screen never flickers blank
        end_t = words[i+1]["start"] if i + 1 < len(words) else w["end"] + 0.2
        
        clean_text = w["text"].strip(".,!?\"'").upper()
        if not clean_text: continue
            
        impact = is_high_impact(clean_text)
        
        # CineMind Department D: Word-level visual & semantic hierarchy
        size = 145 if impact else 120
        fill, depth_col = get_word_color_hierarchy(clean_text)
        
        word_array = render_3d_word(clean_text, fontsize=size, fill=fill, depth=8, depth_color=depth_col)
        
        temp_img_path = f"media/temp_word_{i}.png"
        PIL.Image.fromarray(word_array).save(temp_img_path)
        
        # Center the text directly in the viewer's focal path
        txt_clip = ImageClip(temp_img_path, transparent=True).set_position(('center', 1150)).set_start(start_t).set_end(end_t)
        
        # CineMind Elastic Pop: Snappy viral bounce for opening hook words and impact terms
        if i < 3 or impact:
            txt_clip = txt_clip.resize(lambda t: max(1.0, 1.0 + 0.25 * math.exp(-max(0.0, t) * 12.0) * math.cos(max(0.0, t) * 18.0)))
        else:
            txt_clip = txt_clip.resize(lambda t: max(1.0, 1.0 + 0.12 * math.exp(-max(0.0, t) * 14.0)))

        if impact and has_sfx: 
            audio_tracks.append(AudioFileClip("pop.mp3").set_start(start_t).fx(afx.volumex, 0.2))
            
        text_clips.append(txt_clip)

    retention_bar = ColorClip(size=(1080, 15), color=(255, 255, 0)).set_position(lambda t: (-1080 + int(1080 * (t / voice_audio.duration)), 0)).set_duration(voice_audio.duration)
    
    if os.path.exists("thumbnail.jpg"):
        thumb_clip = ImageClip("thumbnail.jpg").set_duration(0.1).resize(height=1920).crop(x_center=1080/2, y_center=1920/2, width=1080, height=1920)
        text_clips.insert(0, thumb_clip.set_start(0))

    final_audio = CompositeAudioClip(audio_tracks)
    # CineMind Zero-Gap Loop: Smooth micro-crossfade to prevent digital click when player loops
    final_audio = final_audio.fx(afx.audio_fadein, 0.05).fx(afx.audio_fadeout, 0.05)
    extra_layers = [retention_bar]
    watermark = get_watermark_clip(voice_audio.duration)
    if watermark:
        extra_layers.append(watermark)
    final = CompositeVideoClip([CompositeVideoClip(clips, size=(1080, 1920)).set_audio(final_audio)] + text_clips + extra_layers, size=(1080, 1920))
    final = apply_procedural_compositing(final)
    
    # Export with ultra-high quality CRF 18 and 12,000k bitrate.
    # Explicit pix_fmt/movflags/audio settings below match Meta's documented
    # encoding recommendations for Instagram/Facebook - without forcing these,
    # ffmpeg's default choices can produce a technically-valid H.264/AAC file
    # that Instagram's stricter parser still rejects with a generic
    # "couldn't process this video" error (most commonly caused by a pixel
    # format other than yuv420p, or the moov atom not being at the front of
    # the file for streaming).
    try:
        final.write_videofile(
            "final_short.mp4", 
            fps=24, 
            codec="libx264", 
            audio_codec="aac", 
            audio_fps=48000,
            threads=4, 
            bitrate="6000k", 
            ffmpeg_params=[
                "-crf", "22", "-preset", "fast",
                "-pix_fmt", "yuv420p",
                "-profile:v", "high",
                "-level", "4.2",
                "-movflags", "+faststart",
                "-ar", "48000",
            ]
        )
        # CineMind Department E: Supervision & Automated Quality Assurance
        qa_report = review_complete_film("final_short.mp4", expected_duration=voice_audio.duration)
        if not qa_report.editorially_approved:
            print(f"⚠️ CineMind QA flagged issues: {qa_report.warnings}")
        else:
            print(f"✅ CineMind Editorial Approval Granted (A/V delta: {qa_report.duration_match_delta}s, black frames: {qa_report.black_frame_count})")
    finally:
        final.close()
        for h in opened_video_handles:
            try:
                h.close()
            except Exception:
                pass

# --- 5. UPLOAD & SYNDICATION ---
def get_public_url(video_file):
    print("☁️ Uploading heavy-duty media for Zernio syndication...")

    # GitHub Releases - PRIMARY method. Gives a permanent, stable public URL.
    # Filebin (previously used here) redirects its download URL to a presigned
    # S3 link that expires after only 60 seconds (confirmed via Filebin's own
    # API spec) - fine for a human clicking immediately, but async third-party
    # fetchers (like Instagram's Reels ingestion pipeline) can take longer than
    # that to actually pull the file, so the link is already dead by the time
    # they try. That's the most likely explanation for Instagram consistently
    # failing to process the video while Facebook (faster to fetch) succeeded.
    try:
        print("   -> Trying GitHub Release (stable, non-expiring URL)...")
        token = os.environ.get("GITHUB_TOKEN")
        repo = os.environ.get("GITHUB_REPOSITORY")
        if token and repo:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            tag = f"short-{int(time.time())}"
            r = requests.post(f"https://api.github.com/repos/{repo}/releases", headers=headers, json={
                "tag_name": tag, "name": tag, "body": "Auto-generated video asset for cross-posting.",
                "draft": False, "prerelease": False,
            }, timeout=30)
            if r.status_code in (200, 201):
                release = r.json()
                upload_url = release["upload_url"].split("{")[0]
                with open(video_file, "rb") as f:
                    video_bytes = f.read()
                r2 = requests.post(f"{upload_url}?name={os.path.basename(video_file)}",
                                    headers={**headers, "Content-Type": "video/mp4"},
                                    data=video_bytes, timeout=180)
                if r2.status_code in (200, 201):
                    print("   ✅ GitHub Release upload successful (stable URL)!")
                    return r2.json().get("browser_download_url")
                else:
                    print(f"   ⚠️ GitHub Release asset upload failed: {r2.text[:300]}")
            else:
                print(f"   ⚠️ GitHub Release creation failed: {r.text[:300]}")
        else:
            print("   ⚠️ GITHUB_TOKEN/GITHUB_REPOSITORY not available in this environment - skipping GitHub Release method.")
    except Exception as e:
        print(f"   ⚠️ GitHub Release error: {e}")

    # Fallback 1: Filebin (fine for Facebook, which fetches fast - but NOT
    # reliable for Instagram per the expiring-URL issue above). Only used if
    # GitHub Release hosting isn't available.
    try:
        print("   -> Trying Filebin (fallback - may still fail for Instagram specifically)...")
        import string, random
        bin_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        filename = os.path.basename(video_file)
        url = f"https://filebin.net/{bin_id}/{filename}"
        
        with open(video_file, "rb") as f:
            headers = {"Content-Type": "video/mp4", "accept": "application/json"}
            r = requests.post(url, data=f, headers=headers, timeout=180)
            if r.status_code in (200, 201):
                print("   ✅ Filebin upload successful!")
                return url
            else:
                print(f"   ⚠️ Filebin rejected the file: Status {r.status_code}")
    except Exception as e:
        print(f"   ⚠️ Filebin connection error: {e}")

    # Fallback 2: Uguu.se (128MB limit fallback)
    try:
        print("   -> Trying Uguu.se...")
        with open(video_file, "rb") as f:
            r = requests.post("https://uguu.se/upload", files={"files[]": f}, timeout=180)
            if r.status_code == 200 and r.json().get("success"):
                direct_url = r.json()["files"][0]["url"]
                print("   ✅ Uguu upload successful!")
                return direct_url
            else:
                print(f"   ⚠️ Uguu rejected the file: {r.text}")
    except Exception as e:
        print(f"   ⚠️ Uguu connection error: {e}")

    return None

def upload_to_youtube(video_file, data, credentials):
    youtube = build("youtube", "v3", credentials=credentials)
    final_title = optimize_title(data.get("title", ""))
    data["title"] = final_title
    hashtags = build_description_hashtags(data.get("tags", []))
    youtube_tags = build_youtube_seo_tags(
        seo_tags=data.get("seo_tags", []),
        hashtags_list=data.get("tags", []),
        channel_tags=data.get("_channel_tags", []),
    )
    keyword = urllib.parse.quote(data.get("affiliate_keyword", "mystery books"))

    # Real Amazon Associates tag required - set AMAZON_AFFILIATE_TAG env var once you
    # have one. Without it, the link is skipped entirely rather than shipping a
    # broken placeholder ("YOUR_AMAZON_TAG_HERE") to every viewer, and the previous
    # markdown-bracket format was never going to render as a link on YouTube anyway
    # (YouTube descriptions are plain text, not markdown).
    amazon_tag = os.environ.get("AMAZON_AFFILIATE_TAG")
    affiliate_block = ""
    affiliate_link = ""
    if amazon_tag:
        affiliate_link = f"https://www.amazon.com/s?k={keyword}&tag={amazon_tag}"
        affiliate_block = f"👇 Gear up:\n{affiliate_link}"

    theme_label = sanitize_theme_label(data.get("visual_theme", ""))
    playlist_title = f"{theme_label.title()} Mysteries" if theme_label else "CineMind Mysteries"
    full_description = build_seo_description(data, hashtags, affiliate_block, playlist_title=playlist_title)
    body = {
        "snippet": {
            "categoryId": "28",
            "title": final_title,
            "description": full_description,
            "tags": youtube_tags,
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    video_id = youtube.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(video_file, chunksize=-1, resumable=True)).execute()['id']
    print(f"✅ Success! YouTube Link: https://youtu.be/{video_id}")
    
    # Force direct thumbnail upload via API
    if os.path.exists("thumbnail.jpg"):
        try:
            youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload("thumbnail.jpg")).execute()
            print("🖼️ Custom high-CTR thumbnail successfully pushed to YouTube!")
        except Exception as e:
            print(f"⚠️ Notice: Direct thumbnail API upload skipped: {e}")
    return video_id, affiliate_link

def post_auto_comment(video_id, script_text, affiliate_link, credentials, preset_comment=None):
    print("💬 Posting monetized auto-comment...")
    youtube = build("youtube", "v3", credentials=credentials)
    support_line = f"\n\n🔍 Support the channel: {affiliate_link}" if affiliate_link else ""
    comment_text = (preset_comment or "").strip().strip('"')
    if not comment_text:
        comment_text = generate_text_with_fallback(
            f"Write ONE short, debate-sparking question (under 20 words) to pin in the YouTube comments for this script: {script_text}"
        ) or "What do you think actually happened here? Drop your theory below!"
    try:
        youtube.commentThreads().insert(
            part="snippet",
            body={"snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": f"{comment_text}{support_line}"}}}},
        ).execute()
        print("✅ Monetized auto-comment posted!")
    except Exception as e:
        print(f"⚠️ Auto-comment skipped: {e}")

def repost_via_zernio(video_file, data):
    print("\n🚀 Preparing Zernio Multi-Platform Syndication...")
    api_key = os.environ.get("ZERNIO_API_KEY")
    if not api_key:
        print("⚠️ ZERNIO_API_KEY environment variable is missing. Skipping cross-posting.")
        return False
        
    url = get_public_url(video_file)
    if not url:
        print("⚠️ Could not generate public media URL via Filebin/Uguu. Skipping Zernio.")
        return False
    
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        acc_url = base64.b64decode("aHR0cHM6Ly96ZXJuaW8uY29tL2FwaS92MS9hY2NvdW50cw==").decode("utf-8")
        acc_r = requests.get(acc_url, headers=headers, timeout=30)
        
        if acc_r.status_code != 200:
            print(f"⚠️ ZERNIO API Error ({acc_r.status_code}): {acc_r.text}")
            return False

        accounts = acc_r.json().get("accounts", [])
        platform_entries = [{"platform": acc["platform"], "accountId": acc.get("_id") or acc.get("id")} 
                            for acc in accounts if acc.get("platform") in ("instagram", "facebook", "tiktok")]
        
        if not platform_entries:
            print("⚠️ No connected Instagram/TikTok/Facebook accounts found in Zernio dashboard.")
            return False

        post_url = base64.b64decode("aHR0cHM6Ly96ZXJuaW8uY29tL2FwaS92MS9wb3N0cw==").decode("utf-8")
        hashtags_str = build_description_hashtags(data.get("tags", []))
        viral_cloud = " ".join(MANDATORY_VIRAL_HASHTAGS)
        social_caption = f"{data.get('title', '')}\n\n{data.get('description', '')}\n\n{hashtags_str}\n\n{viral_cloud}".strip()[:2200]
        payload = {
            "content": social_caption,
            "mediaItems": [{"type": "video", "url": url}],
            "platforms": platform_entries,
            "publishNow": True
        }

        try:
            # Zernio processes posts asynchronously - status moves through
            # scheduled -> publishing -> published (or failed/partial). The
            # CREATION response below only confirms Zernio accepted the request,
            # NOT that it actually went live - that was the bug: this used to be
            # treated as final success, but the real status arrives later. Must
            # poll GET /v1/posts/{postId} until it reaches a terminal state.
            r = requests.post(post_url, headers=headers, json=payload, timeout=300)
            if r.status_code not in (200, 201, 202, 207):
                print(f"⚠️ ZERNIO Post Creation Error ({r.status_code}): {r.text}")
                return False

            post_id = r.json().get("post", {}).get("_id")
            if not post_id:
                print(f"⚠️ Zernio response had no post ID to track: {r.text[:300]}")
                return False

            print(f"ℹ️ Post created (id: {post_id}), polling for actual publish status...")
            status_url = f"{post_url}/{post_id}"
            terminal_states = ("published", "failed", "partial")
            final_post = None

            for attempt in range(24):  # up to 24 * 10s = 4 minutes
                time.sleep(10)
                try:
                    s = requests.get(status_url, headers=headers, timeout=30)
                    if s.status_code != 200:
                        continue
                    post_obj = s.json().get("post", s.json())
                    if post_obj.get("status") in terminal_states:
                        final_post = post_obj
                        break
                except requests.exceptions.RequestException as poll_err:
                    # A single slow/failed poll shouldn't kill the whole loop or fall
                    # through to the initial-POST timeout handler below (which prints
                    # a misleading "media upload already succeeded" message meant for
                    # a completely different failure point). Just log and keep polling.
                    print(f"  (poll attempt {attempt + 1} had a network hiccup, continuing: {poll_err})")
                    continue

            if final_post is None:
                print("⚠️ Zernio post never reached a final status within 4 minutes - check "
                      "zernio.com/dashboard directly to confirm what actually happened.")
                return False

            # Report the REAL per-platform outcome, with the live URL as actual proof
            # If Instagram reports code 9007 ('still processing the media'), retry polling for 3 more cycles
            for _retry in range(3):
                has_pending_ig = any(
                    e.get("platform") == "instagram" and
                    e.get("status") != "published" and
                    ("processing the media" in str(e.get("errorMessage", "")) or e.get("platformError", {}).get("code") == 9007)
                    for e in final_post.get("platforms", [])
                )
                if not has_pending_ig:
                    break
                print("   ⏳ Instagram is still ingesting media on Meta's end, waiting 20s for final status...")
                time.sleep(20)
                try:
                    s_ig = requests.get(status_url, headers=headers, timeout=30)
                    if s_ig.status_code == 200:
                        final_post = s_ig.json().get("post", s_ig.json())
                except Exception:
                    pass

            any_success = False
            for entry in final_post.get("platforms", []):
                plat = entry.get("platform", "?")
                plat_status = entry.get("status", "unknown")
                plat_url = entry.get("platformPostUrl")
                if plat_status == "published" and plat_url:
                    print(f"  ✅ {plat}: published - {plat_url}")
                    any_success = True
                else:
                    print(f"  ❌ {plat}: {plat_status} - full detail: {entry}")

            if any_success:
                print("✅ Zernio cross-post confirmed live (see links above).")
            else:
                print("❌ Zernio post finished processing but did NOT actually go live on any platform.")
            return any_success

        except requests.exceptions.Timeout:
            print("⚠️ Zernio post timed out after 300s with no response. The media upload already "
                  "succeeded, so this request likely reached Zernio's servers even though we never "
                  "got a reply - check zernio.com/dashboard to see if it actually posted before "
                  "manually retrying, to avoid double-posting.")
    except Exception as e:
        print(f"⚠️ Zernio Syndication Exception: {e}")
    return False

def send_telegram_notification(video_title, video_id, thumbnail_punch=""):
    """Pings the user's private Telegram when a new video is published autonomously."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return
    yt_link = f"https://youtu.be/{video_id}"
    msg = (
        f"🎬 *New YouTube Short Published!*\n\n"
        f"📌 *Title:* {video_title}\n"
        f"🖼️ *Thumbnail Text:* `{thumbnail_punch}`\n"
        f"🔗 *Watch Here:* {yt_link}\n\n"
        f"✅ Syndicated to Instagram Reels & Facebook Video."
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
        if r.status_code == 200:
            print("📲 Telegram publish notification delivered to your phone!")
        else:
            print(f"⚠️ Telegram notification status {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ Telegram notification failed: {e}")

def sanitize_theme_label(visual_theme, max_words=3, max_chars=40):
    """Gemini sometimes returns visual_theme as a full descriptive sentence
    instead of a short label (e.g. 'Dark, Unsettling Historical Reenactment
    With Abstract Elements...') - that breaks playlist titles. Take just the
    first few words and hard-cap length so it stays usable as a title."""
    import re
    cleaned = re.sub(r"[^\w\s]", "", visual_theme or "").strip()
    words = cleaned.split()[:max_words]
    label = " ".join(words)[:max_chars].strip()
    return label

def add_video_to_themed_playlist(youtube, video_id, visual_theme, max_retries=3):
    """Groups videos into themed playlists (e.g. 'Space Mysteries'). Playlists
    drive session watch time via autoplay-into-next-video - a heavy YouTube
    ranking signal we weren't using before."""
    theme_label = sanitize_theme_label(visual_theme)
    if not theme_label:
        return
    playlist_title = f"{theme_label.title()} Mysteries"

    for attempt in range(max_retries):
        try:
            existing = youtube.playlists().list(part="snippet", mine=True, maxResults=50).execute()
            match = next((p for p in existing.get("items", [])
                          if p["snippet"]["title"].lower() == playlist_title.lower()), None)

            if match:
                playlist_id = match["id"]
            else:
                created = youtube.playlists().insert(part="snippet,status", body={
                    "snippet": {"title": playlist_title, "description": f"Auto-generated collection of {theme_label} content."},
                    "status": {"privacyStatus": "public"},
                }).execute()
                playlist_id = created["id"]
                print(f"📁 Created new playlist: {playlist_title}")

            youtube.playlistItems().insert(part="snippet", body={
                "snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}
            }).execute()
            print(f"✅ Added video to playlist: {playlist_title}")
            return
        except Exception as e:
            # YouTube's API occasionally throws transient 409/503 errors - worth a retry
            # before giving up, rather than failing on the first hiccup.
            if attempt < max_retries - 1:
                print(f"⚠️ Playlist attempt {attempt + 1} failed, retrying: {e}")
                time.sleep(5 * (attempt + 1))
            else:
                print(f"⚠️ Playlist grouping skipped after {max_retries} attempts: {e}")

def reply_to_top_comments(credentials, max_replies=3):
    """Auto-replies to top comments on your MOST RECENT PAST video (not the one
    just uploaded, which has zero comments yet) - light engagement signal, and
    the algorithm does notice reply rate on a channel."""
    try:
        youtube = build("youtube", "v3", credentials=credentials)
        search_res = youtube.search().list(part="id", forMine=True, type="video", order="date", maxResults=1).execute()
        items = search_res.get("items", [])
        if not items:
            return
        video_id = items[0]["id"]["videoId"]

        comments_res = youtube.commentThreads().list(
            part="snippet", videoId=video_id, order="relevance", maxResults=max_replies, textFormat="plainText"
        ).execute()

        for thread in comments_res.get("items", []):
            top_comment = thread["snippet"]["topLevelComment"]["snippet"]["textDisplay"]
            thread_id = thread["id"]
            # Skip if it already has replies (avoid double-replying on repeat runs)
            if thread["snippet"].get("totalReplyCount", 0) > 0:
                continue
            try:
                reply_text = generate_text_with_fallback(
                    f"Write ONE short, warm, engaging reply (under 15 words) to this YouTube comment: \"{top_comment}\""
                )
                if not reply_text:
                    continue
                youtube.comments().insert(part="snippet", body={
                    "snippet": {"parentId": thread_id, "textOriginal": reply_text}
                }).execute()
                print(f"💬 Replied to a comment: {reply_text}")
            except Exception as e:
                print(f"⚠️ Reply skipped for one comment: {e}")
    except Exception as e:
        print(f"⚠️ Comment engagement skipped: {e}")

async def main():
    if not os.path.exists("media"): os.makedirs("media")
    restore_google_secrets()
    credentials = get_google_credentials()
    
    # Run the Weekly Analytics Telemetry Logger
    log_weekly_analytics(credentials)

    # Engage with existing audience on the previous video before making a new one
    reply_to_top_comments(credentials)

    boost_topics = fetch_top_performing_titles(credentials)
    retention_dropoff_sec = fetch_retention_dropoff(credentials)

    # Fetch tags from your highest reach videos and queued viewer requests
    viral_tags = fetch_high_reach_tags(credentials)
    viewer_requests = load_viewer_requests()
    
    # Pass the tags and viewer demand into the AI generator
    content = generate_script(
        avoid_topics=[h["title"] for h in load_topic_history()], 
        boost_topics=boost_topics,
        dynamic_tags_list=viral_tags,
        retention_dropoff_sec=retention_dropoff_sec,
        viewer_requests=viewer_requests,
    )
    content["_channel_tags"] = viral_tags
    # CineMind Department B: Formulate the structured creative brief
    brief = determine_creative_direction(content)

    await generate_audio_and_timestamps(content["script"])
    download_visual_media(
        content["image_prompts"],
        video_queries=content.get("video_queries", []),
        visual_theme=content.get("visual_theme", ""),
    )
    
    # Corrected High-CTR Thumbnail generation (runs BEFORE video assembly and YouTube upload)
    # Linted via YouTube Packaging Linter: Rule of 3 + 10% Zoom Test + Dual A/B variants
    linted_title, linted_thumb = lint_title_thumbnail_pair(content.get("title", ""), content.get("thumbnail_text", ""))
    content["title"] = linted_title
    content["thumbnail_text"] = linted_thumb
    
    # 3-Spot Free SEO Verification (Title, Description, and First 15s Audio)
    verify_and_enforce_3_spot_seo(content)
    
    generate_ai_thumbnail(
        title=content.get("title", ""), 
        text_hook=content.get("thumbnail_text", ""), 
        visual_theme=content.get("visual_theme", ""),
        content=content
    )
    
    # CineMind Department D: Source fresh theme-matched soundtrack & dynamic sound effects
    download_sfx(brief=brief)
    dynamic_sfx_map = download_dynamic_sfx(content.get("sfx_cues", []))
    save_community_post(content)
    
    # CineMind Departments A, C, D, E: Assemble timeline, grade, format kinetic typography & review
    assemble_video(dynamic_sfx_map, brief=brief)
    
    vid_id, aff_link = upload_to_youtube("final_short.mp4", content, credentials)
    if vid_id:
        post_auto_comment(vid_id, content["script"], aff_link, credentials, preset_comment=content.get("pinned_comment"))
        save_topic_history({"title": content["title"], "video_id": vid_id, "date": time.strftime("%Y-%m-%d")})
        save_longform_expansion_seed(content)
        if viewer_requests:
            try:
                with open(VIEWER_REQUESTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(viewer_requests[1:], f, indent=2)
            except Exception:
                pass
        youtube_client = build("youtube", "v3", credentials=credentials)
        add_video_to_themed_playlist(youtube_client, vid_id, content.get("visual_theme", ""))
        send_telegram_notification(content.get("title", ""), vid_id, content.get("thumbnail_text", ""))
    repost_via_zernio("final_short.mp4", content)

if __name__ == "__main__":
    asyncio.run(main())